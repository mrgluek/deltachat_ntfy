"""1.6.9: the invite link (a new core invite token per call) is read once, not per message or request."""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("DB_PATH", "test_ntfy.db")

import bot
import database
import dc_fingerprint

SELF_FP = "D58B5E6C55482F94F9F5B84B6FB9FE3AD2658082"
ADMIN_FP = "DEC0E8D11FC4686F2C715C56F8C9AD699BC19AD8"
QR = f"https://i.delta.chat/#{SELF_FP}&v=3&i=inv&s=auth"
INFO = f"Fingerprints:\n\nMe:\n{SELF_FP}\n\nAdmin:\n{ADMIN_FP}\n\nRelays:\nadmin@example.net"


def make_rpc():
    rpc = MagicMock()
    rpc.get_chat_securejoin_qr_code.return_value = QR
    rpc.get_contact_encryption_info.return_value = INFO
    return rpc


def web_request(topic="alerts"):
    req = MagicMock()
    req.match_info = {"topic": topic}
    req.headers = {}
    req.query = {}
    req.path = "/" + topic
    req.remote = "203.0.113.100"
    return req


class TestInviteLinkCache(unittest.TestCase):
    def setUp(self):
        database.init_db()
        dc_fingerprint.forget_self_fingerprint()
        bot.bot_qr_cache.clear()
        self.addCleanup(bot.bot_qr_cache.clear)

    def test_1000_lookups_cost_one_qr_call(self):
        rpc = make_rpc()
        for _ in range(1000):
            self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), SELF_FP)
        self.assertEqual(rpc.get_chat_securejoin_qr_code.call_count, 1)

    def test_failure_is_not_cached(self):
        rpc = make_rpc()
        rpc.get_chat_securejoin_qr_code.side_effect = [Exception("down"), QR]
        self.assertIsNone(dc_fingerprint.get_self_fingerprint(rpc, 1))
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), SELF_FP)

    def test_1000_admin_checks_through_the_handler_cost_one_qr_call(self):
        database.set_config("admin_dc_fingerprint", ADMIN_FP)
        b = MagicMock()
        b.rpc = make_rpc()
        for _ in range(1000):
            self.assertTrue(bot._is_dc_admin(b, 1, 10))
        self.assertLessEqual(b.rpc.get_chat_securejoin_qr_code.call_count, 1)

    def test_1000_topic_page_requests_cost_one_qr_call(self):
        rpc = make_rpc()
        inst = MagicMock()
        inst.rpc = rpc
        with patch("bot.dc_bot_instance", inst), patch("bot.dc_accid", 1), \
                patch("bot.check_rate_limit", return_value=True), \
                patch("bot.database.get_recent_notifications", return_value=[]):
            for _ in range(1000):
                page = asyncio.run(bot.handle_topic_view(web_request())).text
        self.assertLessEqual(rpc.get_chat_securejoin_qr_code.call_count, 1)
        self.assertIn(QR, page)

    def test_topic_page_failure_is_not_cached(self):
        rpc = make_rpc()
        rpc.get_chat_securejoin_qr_code.side_effect = [Exception("down"), QR]
        inst = MagicMock()
        inst.rpc = rpc
        with patch("bot.dc_bot_instance", inst), patch("bot.dc_accid", 1), \
                patch("bot.check_rate_limit", return_value=True), \
                patch("bot.database.get_recent_notifications", return_value=[]):
            first = asyncio.run(bot.handle_topic_view(web_request())).text
            second = asyncio.run(bot.handle_topic_view(web_request())).text
        self.assertNotIn(QR, first)
        self.assertIn(QR, second)


if __name__ == "__main__":
    unittest.main()
