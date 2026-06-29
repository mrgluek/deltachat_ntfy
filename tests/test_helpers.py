import os
import sys
import unittest
from unittest.mock import MagicMock

# Set DB_PATH to a temporary test file
TEST_DB = "test_ntfy.db"
os.environ["DB_PATH"] = TEST_DB

# Mock packages if not installed
try:
    import deltachat2
except ImportError:
    sys.modules['deltachat2'] = MagicMock()
try:
    import deltabot_cli
except ImportError:
    sys.modules['deltabot_cli'] = MagicMock()
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
