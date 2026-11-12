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
