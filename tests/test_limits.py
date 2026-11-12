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
