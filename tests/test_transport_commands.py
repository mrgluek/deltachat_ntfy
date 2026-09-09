"""
Tests for transport and admin commands in deltachat_ntfy.
Verifies private chat enforcement on /addtransport and /initadmin,
resilient mode commands, and error sanitization.
"""
import os
import unittest
from unittest.mock import MagicMock, patch
import sys

# Ensure root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock dependencies if missing
try:
    import deltachat2
except ImportError:
    mock_deltachat2 = MagicMock()
    class MsgData:
        def __init__(self, text="", file="", override_sender_name=None):
            self.text = text
            self.file = file
            self.override_sender_name = override_sender_name
    mock_deltachat2.MsgData = MsgData
    sys.modules['deltachat2'] = mock_deltachat2

try:
    import deltabot_cli
except ImportError:
    class MockBotCli:
        def __init__(self, *args, **kwargs):
            pass
        def on(self, *args, **kwargs):
            return lambda func: func
        def on_init(self, func):
            return func
        def on_start(self, func):
            return func
        def start(self):
            pass
    mock_deltabot_cli = MagicMock()
    mock_deltabot_cli.BotCli = MockBotCli
    sys.modules['deltabot_cli'] = mock_deltabot_cli

try:
    import emoji
except ImportError:
    sys.modules['emoji'] = MagicMock()

try:
    import aiohttp
    from aiohttp import web
except ImportError:
    aiohttp = MagicMock()
    web = MagicMock()
    class MockResponse:
        def __init__(self, text="", status=200, content_type="", **kwargs):
            self.text = text
            self.status = status
            self.content_type = content_type
    web.Response = MockResponse
    web.FileResponse = MagicMock()
    aiohttp.web = web
    sys.modules['aiohttp'] = aiohttp
    sys.modules['aiohttp.web'] = web

import database
import bot

TEST_DB_PATH = "test_ntfy_cmds.db"


class TestTransportCommands(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = database.DB_PATH
        database.DB_PATH = TEST_DB_PATH
        database.init_db()

        self.mock_bot = MagicMock()
        self.mock_event = MagicMock()
        self.mock_event.msg.from_id = 100
        self.mock_event.msg.chat_id = 10
        self.accid = 1

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        for f in (TEST_DB_PATH, f"{TEST_DB_PATH}-wal", f"{TEST_DB_PATH}-shm"):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

    @patch("bot._dc_send_msg_with_stats")
    @patch("bot._is_dc_admin")
    def test_addtransport_requires_admin(self, mock_is_admin, mock_send):
        mock_is_admin.return_value = False
        bot.addtransport_command(self.mock_bot, self.accid, self.mock_event)
        mock_send.assert_called_once()
        msg_text = mock_send.call_args[0][3].text
        self.assertIn("only for the administrator", msg_text)

    @patch("bot._dc_send_msg_with_stats")
    @patch("bot._is_private_chat")
    @patch("bot._is_dc_admin")
    def test_addtransport_rejected_in_group_chat(self, mock_is_admin, mock_is_private, mock_send):
        mock_is_admin.return_value = True
        mock_is_private.return_value = False
        self.mock_event.payload = "user@example.com mySecretPass"

        bot.addtransport_command(self.mock_bot, self.accid, self.mock_event)

        mock_send.assert_called_once()
        msg_text = mock_send.call_args[0][3].text
        self.assertIn("only be used in a private 1:1 chat", msg_text)
        self.mock_bot.rpc.add_or_update_transport.assert_not_called()

    @patch("bot._dc_send_msg_with_stats")
    @patch("bot._is_private_chat")
    @patch("bot._is_dc_admin")
    def test_addtransport_allowed_in_private_chat(self, mock_is_admin, mock_is_private, mock_send):
        mock_is_admin.return_value = True
        mock_is_private.return_value = True
        self.mock_event.payload = "backup@example.com mySecretPass"

        bot.addtransport_command(self.mock_bot, self.accid, self.mock_event)

        self.mock_bot.rpc.add_or_update_transport.assert_called_once_with(
            self.accid, {"addr": "backup@example.com", "password": "mySecretPass"}
        )
        mock_send.assert_called_once()
        msg_text = mock_send.call_args[0][3].text
        self.assertIn("Backup transport `backup@example.com` added", msg_text)

    @patch("bot._dc_send_msg_with_stats")
    @patch("bot._is_private_chat")
    @patch("bot._is_dc_admin")
    def test_addtransport_error_is_sanitized(self, mock_is_admin, mock_is_private, mock_send):
        mock_is_admin.return_value = True
        mock_is_private.return_value = True
        self.mock_event.payload = "backup@example.com mySecretPass"
        self.mock_bot.rpc.add_or_update_transport.side_effect = RuntimeError("Internal db timeout / secret path /var/run/secret")

        bot.addtransport_command(self.mock_bot, self.accid, self.mock_event)

        mock_send.assert_called_once()
        msg_text = mock_send.call_args[0][3].text
        self.assertIn("Failed to add transport. Check server logs", msg_text)
        self.assertNotIn("secret path", msg_text)

    @patch("bot._dc_send_msg_with_stats")
    @patch("bot._is_private_chat")
    def test_initadmin_rejected_in_group_chat(self, mock_is_private, mock_send):
        mock_is_private.return_value = False
        bot.initadmin_command(self.mock_bot, self.accid, self.mock_event)
        mock_send.assert_called_once()
        msg_text = mock_send.call_args[0][3].text
        self.assertIn("only be used in a private 1:1 chat", msg_text)

    @patch("bot._dc_send_msg_with_stats")
    @patch("bot._is_private_chat")
    def test_initadmin_success(self, mock_is_private, mock_send):
        mock_is_private.return_value = True
        mock_contact = MagicMock()
        mock_contact.address = "admin@example.com"
        self.mock_bot.rpc.get_contact.return_value = mock_contact

        with patch("bot._get_contact_fingerprint", return_value="AABBCCDDEEFF0011"):
            bot.initadmin_command(self.mock_bot, self.accid, self.mock_event)

        self.assertEqual(database.get_config("admin_dc_email"), "admin@example.com")
        self.assertEqual(database.get_admin_fingerprint(), "AABBCCDDEEFF0011")
        mock_send.assert_called_once()
        msg_text = mock_send.call_args[0][3].text
        self.assertIn("You are now the admin", msg_text)

    @patch("bot._dc_send_msg_with_stats")
    @patch("bot._is_dc_admin")
    def test_resilient_command(self, mock_is_admin, mock_send):
        mock_is_admin.return_value = True

        self.mock_event.payload = "on"
        bot.resilient_command(self.mock_bot, self.accid, self.mock_event)
        self.assertEqual(database.get_config("resilient"), "1")

        self.mock_event.payload = "off"
        bot.resilient_command(self.mock_bot, self.accid, self.mock_event)
        self.assertEqual(database.get_config("resilient"), "0")


if __name__ == "__main__":
    unittest.main()
