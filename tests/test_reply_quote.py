import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

TEST_DB = "test_reply_quote.db"
os.environ["DB_PATH"] = TEST_DB

try:
    import deltachat2  # noqa: F401
except ImportError:
    sys.modules['deltachat2'] = MagicMock()
try:
    import deltabot_cli  # noqa: F401
except ImportError:
    class MockBotCli:
        def __init__(self, *a, **k):
            pass
        def on(self, *a, **k):
            return lambda f: f
        def on_init(self, f):
            return f
        def on_start(self, f):
            return f
        def start(self):
            pass
    m = MagicMock()
    m.BotCli = MockBotCli
    sys.modules['deltabot_cli'] = m

import database
import bot


class TestTopicFromQuote(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = TEST_DB
        database.init_db()
        database.subscribe(5, "otdru")

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(TEST_DB + suffix)
            except FileNotFoundError:
                pass

    def test_private_prefix(self):
        q = SimpleNamespace(text="`[news]` \n\nhello")
        self.assertEqual(bot._topic_from_quote(q, 1), "news")

    def test_group_override_sender_name(self):
        q = SimpleNamespace(text="hello", override_sender_name="#otdru", author_display_name="x")
        self.assertEqual(bot._topic_from_quote(q, 5), "otdru")

    def test_group_author_display_name_with_tilde(self):
        q = SimpleNamespace(text="hello", author_display_name="~#otdru")
        self.assertEqual(bot._topic_from_quote(q, 5), "otdru")

    def test_group_not_subscribed(self):
        q = SimpleNamespace(text="hello", override_sender_name="#otdru")
        self.assertIsNone(bot._topic_from_quote(q, 99))

    def test_plain_quote(self):
        q = SimpleNamespace(text="hello", author_display_name="Alice")
        self.assertIsNone(bot._topic_from_quote(q, 5))


class TestAttachmentViewtype(unittest.TestCase):
    def test_images(self):
        self.assertEqual(bot.attachment_viewtype("/tmp/a.webp"), "Image")
        self.assertEqual(bot.attachment_viewtype("/tmp/a.PNG"), "Image")
        self.assertEqual(bot.attachment_viewtype("/tmp/a.gif"), "Gif")

    def test_other(self):
        self.assertIsNone(bot.attachment_viewtype("/tmp/a.pdf"))
        self.assertIsNone(bot.attachment_viewtype("/tmp/a"))


if __name__ == "__main__":
    unittest.main()
