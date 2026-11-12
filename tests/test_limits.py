import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot


def request(remote='203.0.113.1', headers=None, query=None):
    req = MagicMock(remote=remote)
    req.headers = headers or {}
    req.query = query or {}
    req.match_info = {'topic': 'limits'}
    req.path = '/limits'
    req.read = AsyncMock(return_value=b'hello')
    return req


class TestRateLimits(unittest.TestCase):
    def setUp(self):
        bot._rate_limits.clear()
        bot._rate_last_sweep = 0

    def tearDown(self):
        bot._rate_limits.clear()

    def test_untrusted_peer_ignores_spoof(self):
        self.assertEqual(bot.get_client_ip(request(headers={'X-Forwarded-For': '1.1.1.1'})), '203.0.113.1')

    def test_trusted_chain_uses_rightmost_untrusted(self):
        req = request('127.0.0.1', {'X-Forwarded-For': '1.1.1.1, 8.8.8.8, 10.0.0.2'})
        self.assertEqual(bot.get_client_ip(req), '8.8.8.8')

    def test_invalid_or_all_trusted_header_falls_back(self):
        for value in ('bad, 8.8.8.8', '10.0.0.2', ''):
            self.assertEqual(bot.get_client_ip(request('127.0.0.1', {'X-Forwarded-For': value})), '127.0.0.1')

    def test_ipv6_shared_64(self):
        self.assertFalse(bot.is_rate_limited('2001:db8::1', 1))
        self.assertTrue(bot.is_rate_limited('2001:db8::ffff', 1))
        self.assertFalse(bot.is_rate_limited('2001:db8:0:1::1', 1))

    def test_memory_bounded(self):
        for i in range(100000):
            bot._hit(f'test:{i}', 1)
        self.assertLessEqual(len(bot._rate_limits), bot.MAX_RATE_KEYS)

    def test_sweep_idle(self):
        with patch('bot.time.monotonic', return_value=100):
            bot._hit('idle', 1)
        with patch('bot.time.monotonic', return_value=161):
            bot._hit('new', 1)
        self.assertNotIn('idle', bot._rate_limits)

    def test_post_30_per_real_ip(self):
        with patch.object(bot, 'AUTH_TOKEN', None), patch('bot.database.add_notification'), patch('bot.database.get_subscribers', return_value=[]):
            for i in range(31):
                response = asyncio.run(bot.handle_ntfy_post(request(headers={'X-Forwarded-For': f'1.1.1.{i}'})))
                self.assertEqual(response.status, 200 if i < 30 else 429)


class TestStreams(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot._rate_limits.clear()
        bot.listeners.clear()
        bot._stream_counts.clear()
        bot._stream_total = 0
        self.response = MagicMock()
        self.response.prepare = AsyncMock()
        self.response.write = AsyncMock()
        self.patcher = patch('bot.web.StreamResponse', return_value=self.response)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def test_overflow_only_closes_slow_stream(self):
        slow, fast = bot.Stream('a'), bot.Stream('b')
        for _ in range(bot.STREAM_QUEUE_MAX):
            bot._offer(slow, {})
        bot._offer(slow, {'message': 'last'})
        bot._offer(fast, {'message': 'last'})
        self.assertTrue(slow.dropped)
        self.assertFalse(fast.dropped)
        self.assertEqual((await fast.q.get())['message'], 'last')
        task = asyncio.create_task(bot.handle_ntfy_json(request()))
        await self._registered()
        stream = bot.listeners['limits'][0]
        stream.dropped = True
        stream.q.put_nowait({})
        await task
        self.assertIn(b'"event": "close"', self.response.write.call_args.args[0])
        self.assertEqual(bot.listeners, {})
        self.assertEqual(bot._stream_total, 0)

    async def _registered(self):
        for _ in range(30):
            if bot.listeners.get('limits'):
                return
            await asyncio.sleep(0)
        self.fail('stream did not register')

    async def test_disconnect_releases_slot(self):
        task = asyncio.create_task(bot.handle_ntfy_json(request()))
        await self._registered()
        task.cancel()
        await task
        self.assertEqual(bot._stream_total, 0)
        self.assertEqual(bot._stream_counts, {})
        self.assertEqual(bot.listeners, {})

    async def test_prepare_exception_releases_slot(self):
        self.response.prepare.side_effect = RuntimeError('broken')
        with self.assertRaises(RuntimeError):
            await bot.handle_ntfy_json(request())
        self.assertEqual(bot._stream_total, 0)
        self.assertEqual(bot._stream_counts, {})

    async def test_ip_and_total_caps(self):
        for name in ('MAX_STREAMS_PER_IP', 'MAX_STREAMS_TOTAL'):
            with patch.object(bot, name, 0):
                response = await bot.handle_ntfy_json(request())
            self.assertEqual(response.status, 429)
            self.assertEqual(response.headers['Retry-After'], '30')

    async def test_poll_uses_no_slot(self):
        with patch.object(bot, 'MAX_STREAMS_TOTAL', 0):
            await bot.handle_ntfy_json(request(query={'poll': '1'}))
        self.assertEqual(bot._stream_total, 0)
        self.assertEqual(bot.listeners, {})

    async def test_write_timeout_releases_slot(self):
        self.response.write.side_effect = asyncio.TimeoutError
        await bot.handle_ntfy_json(request())
        self.assertEqual(bot._stream_total, 0)
        self.assertEqual(bot.listeners, {})

    async def test_push_missing_topic_does_not_create_key(self):
        bot.push_to_listeners('absent', {})
        self.assertEqual(bot.listeners, {})


class TestPublisherLimits(unittest.TestCase):
    def setUp(self):
        from test_outbox import queued
        self.ob = queued(burst=1000)
        bot._rate_limits.clear()
        self.patchers = [patch.object(bot, 'dc_outbox', self.ob),
                         patch.object(bot, 'dc_bot_instance', MagicMock()),
                         patch.object(bot, 'dc_accid', 1),
                         patch.object(bot, 'AUTH_TOKEN', None),
                         patch('bot.database.add_notification'),
                         patch('bot.database.get_subscribers', return_value=[1, 2])]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(bot._rate_limits.clear)
        bot.dc_bot_instance.rpc.get_all_account_ids.return_value = [1]
        bot.dc_bot_instance.rpc.get_basic_chat_info.return_value = {'chat_type': 'Group'}

    def post(self, headers=None, remote='203.0.113.1'):
        return asyncio.run(bot.handle_ntfy_post(request(remote, headers)))

    def test_backlog_rejected_before_file_creation(self):
        with patch.object(bot, 'OUTBOX_MAX_QUEUED', 1), patch('bot.create_attachment_file') as create:
            response = self.post({'Filename': 'a.png'})
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers['Retry-After'], '60')
        create.assert_not_called()
        self.assertEqual(self.ob.waiting_total(), 0)

    def test_topic_limit_across_ips_and_chat_send(self):
        with patch.object(bot, 'TOPIC_RATE_MAX', 2):
            self.assertEqual(self.post().status, 200)
            b = MagicMock()
            event = MagicMock(payload='limits from chat')
            event.msg.text = '/send limits from chat'
            with patch('bot._dc_send_msg_with_stats'):
                bot.send_command(b, 1, event)
            self.assertEqual(self.post(remote='203.0.113.2').status, 429)
            self.assertEqual(bot.database.add_notification.call_count, 2)

    def test_anonymous_urgent_demoted_after_five_and_resets(self):
        with patch('bot.time.monotonic', return_value=100):
            for _ in range(6):
                self.assertEqual(self.post({'Priority': '5'}).status, 200)
        self.assertEqual(self.ob.waiting(), {'urgent': 10, 'normal': 2})
        with patch('bot.time.monotonic', return_value=161):
            self.assertEqual(self.post({'Priority': '5'}).status, 200)
        self.assertEqual(self.ob.waiting()['urgent'], 12)
        self.assertEqual(bot.database.add_notification.call_args.args[-1], 5)

    def test_authenticated_urgent_not_demoted(self):
        with patch.object(bot, 'AUTH_TOKEN', 'secret'):
            for _ in range(6):
                self.assertEqual(self.post({'Priority': '5', 'X-Auth-Token': 'secret'}).status, 200)
        self.assertEqual(self.ob.waiting(), {'urgent': 12})

    def test_budget_refused_before_file_creation(self):
        with patch.object(bot, 'TEMP_ATTACH_BUDGET', 1), patch('bot.tempfile.mkdtemp') as mkdir:
            self.assertEqual(self.post({'Filename': 'a.png'}).status, 503)
        mkdir.assert_not_called()
        self.assertEqual(bot._temp_attach_used, 0)

    def test_budget_released_after_last_send_even_when_send_fails(self):
        from test_outbox import drain
        self.assertEqual(self.post({'Filename': 'a.png'}).status, 200)
        self.assertEqual(bot._temp_attach_used, 5)
        bot.dc_bot_instance.rpc.send_msg.side_effect = RuntimeError('send failed')
        drain(self.ob)
        self.assertEqual(bot._temp_attach_used, 0)

    def test_download_failure_closes_file_removes_directory_and_releases_budget(self):
        import os
        files = []
        original = bot.create_attachment_file

        def capture(*args, **kwargs):
            f = original(*args, **kwargs)
            files.append(f)
            return f

        async def chunks(_):
            yield b'partial'
            raise RuntimeError('download failed')

        response = MagicMock(status=200)
        response.content.iter_chunked = chunks
        session = MagicMock()
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=session)
        with patch('bot.aiohttp.ClientSession', return_value=client), patch('bot.is_safe_url', return_value=(True, 'OK')), patch('bot.create_attachment_file', side_effect=capture):
            self.assertEqual(self.post({'Attach': 'https://example.com/a'}).status, 200)
        self.assertTrue(files[0].closed)
        self.assertFalse(os.path.exists(os.path.dirname(files[0].name)))
        self.assertEqual(bot._temp_attach_used, 0)

    def test_admin_stats_include_backlog_but_public_daily_stats_do_not(self):
        with patch('bot._is_dc_admin', return_value=True), patch('bot.database.get_notifications_last_24h', return_value=0), patch('bot._dc_send_msg_with_stats') as send:
            bot.stats_command(MagicMock(), 1, MagicMock())
            text = send.call_args.args[3].text
            self.assertIn('Rejected:', text)
            self.assertIn('Temp attachments:', text)
            asyncio.run(bot.publish_stats())
        public = bot.database.add_notification.call_args.args[2]
        self.assertNotIn('Rejected:', public)
        self.assertNotIn('Temp attachments:', public)

    def test_budget_released_after_successful_sends(self):
        from test_outbox import drain
        self.assertEqual(self.post({'Filename': 'ok.png'}).status, 200)
        self.assertEqual(bot._temp_attach_used, 5)
        drain(self.ob)
        self.assertEqual(bot._temp_attach_used, 0)

    def test_fanout_exception_cleans_attachment(self):
        bot.dc_bot_instance.rpc.get_all_account_ids.side_effect = RuntimeError('rpc broken')
        with self.assertRaises(RuntimeError):
            self.post({'Filename': 'ok.png'})
        self.assertEqual(bot._temp_attach_used, 0)

    def test_chat_rejection_does_not_confirm_publication(self):
        event = MagicMock(payload='limits message')
        with patch.object(bot, 'TOPIC_RATE_MAX', 1), patch('bot.confirm_sent') as confirm, patch('bot._dc_send_msg_with_stats'):
            self.assertEqual(self.post().status, 200)
            bot.send_command(MagicMock(), 1, event)
        confirm.assert_not_called()
        self.assertEqual(bot.database.add_notification.call_count, 1)
