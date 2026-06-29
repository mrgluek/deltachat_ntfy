import os
import sys
import unittest
from unittest.mock import MagicMock, patch, ANY

# Set DB_PATH to a temporary test file
TEST_DB = "test_ntfy.db"
os.environ["DB_PATH"] = TEST_DB

# Mock packages if not installed
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

# Configure mock emoji behavior if mocked
import emoji
if isinstance(emoji.emojize, MagicMock):
    def mock_emojize(string, language=None):
        if string in (":warning:", ":skull:"):
            return "⚠️" if string == ":warning:" else "💀"
        return string
    emoji.emojize.side_effect = mock_emojize

# Import actual modules
import database
import bot

class TestHelpers(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = TEST_DB
        database.init_db()

    def tearDown(self):
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except OSError:
                pass

    def test_sanitize_string(self):
        self.assertEqual(bot.sanitize_string("hello"), "hello")
        self.assertEqual(bot.sanitize_string(123), 123)
        
        # Check that surrogate string doesn't raise exception
        surrogate_str = "hello\udcffworld"
        sanitized = bot.sanitize_string(surrogate_str)
        self.assertIsNotNone(sanitized)

    def test_get_priority_emoji(self):
        self.assertEqual(bot.get_priority_emoji("5"), "🚨")
        self.assertEqual(bot.get_priority_emoji("max"), "🚨")
        self.assertEqual(bot.get_priority_emoji("urgent"), "🚨")
        self.assertEqual(bot.get_priority_emoji("4"), "⚠️")
        self.assertEqual(bot.get_priority_emoji("high"), "⚠️")
        self.assertEqual(bot.get_priority_emoji("3"), "✅")
        self.assertEqual(bot.get_priority_emoji("default"), "✅")
        self.assertEqual(bot.get_priority_emoji("2"), "ℹ️")
        self.assertEqual(bot.get_priority_emoji("1"), "💤")
        self.assertEqual(bot.get_priority_emoji("invalid"), "✅")

    def test_linkify(self):
        self.assertEqual(bot.linkify(""), "")
        self.assertEqual(bot.linkify(None), "")
        self.assertEqual(bot.linkify("<script>alert(1)</script>"), "&lt;script&gt;alert(1)&lt;/script&gt;")
        
        text = "Check out https://ntfy.gluek.info/test for info"
        expected = 'Check out <a href="https://ntfy.gluek.info/test" target="_blank" rel="noopener noreferrer">https://ntfy.gluek.info/test</a> for info'
        self.assertEqual(bot.linkify(text), expected)

    def test_parse_priority(self):
        self.assertEqual(bot.parse_priority("5"), 5)
        self.assertEqual(bot.parse_priority("max"), 5)
        self.assertEqual(bot.parse_priority("urgent"), 5)
        self.assertEqual(bot.parse_priority("4"), 4)
        self.assertEqual(bot.parse_priority("high"), 4)
        self.assertEqual(bot.parse_priority("3"), 3)
        self.assertEqual(bot.parse_priority("default"), 3)
        self.assertEqual(bot.parse_priority("2"), 2)
        self.assertEqual(bot.parse_priority("low"), 2)
        self.assertEqual(bot.parse_priority("1"), 1)
        self.assertEqual(bot.parse_priority("min"), 1)
        self.assertEqual(bot.parse_priority("unknown"), 3)

    def test_parse_tags(self):
        self.assertEqual(bot.parse_tags(""), ([], []))
        self.assertEqual(bot.parse_tags(None), ([], []))
        
        emojis, text_tags = bot.parse_tags("warning,custom_tag,skull")
        self.assertIn("custom_tag", text_tags)
        self.assertIn("⚠️", emojis)

    @patch('bot._is_dc_admin')
    @patch('bot._dc_send_msg_with_stats')
    def test_url_command_admin(self, mock_send_with_stats, mock_is_admin):
        # 1. Test non-admin access
        mock_is_admin.return_value = False
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.from_id = 456
        mock_event.msg.chat_id = 789
        
        bot.url_command(mock_bot, 1, mock_event)
        
        # Verify it rejects with administrator error message
        mock_bot.rpc.send_msg.assert_called_once_with(
            1, 789, ANY
        )
        msg_data = mock_bot.rpc.send_msg.call_args[0][2]
        self.assertIn("only for the administrator", msg_data.text)
        
        # 2. Test admin access, payload is empty (query current URL)
        mock_bot.reset_mock()
        mock_is_admin.return_value = True
        mock_event.payload = ""
        
        # Set config in DB
        database.set_config("bot_url", "https://my-test-url.com")
        bot.url_command(mock_bot, 1, mock_event)
        
        # Verify it sends current url with stats
        mock_send_with_stats.assert_called_once()
        sent_msg = mock_send_with_stats.call_args[0][3]
        self.assertIn("Current Bot URL: https://my-test-url.com", sent_msg.text)
        
        # 3. Test admin access, payload has new URL
        mock_send_with_stats.reset_mock()
        mock_event.payload = "https://new-url.com"
        
        bot.url_command(mock_bot, 1, mock_event)
        
        # Verify database config updated
        self.assertEqual(database.get_config("bot_url"), "https://new-url.com")
        # Verify it sends success message with stats
        mock_send_with_stats.assert_called_once()
        sent_msg = mock_send_with_stats.call_args[0][3]
        self.assertIn("Bot URL updated to: https://new-url.com", sent_msg.text)
