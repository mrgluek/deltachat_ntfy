"""Tests for /newchannel, /ntfychannels, /ntfychanneldel and the channels table."""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

TEST_DB = "test_channels.db"
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

GONE = Exception("{'code': -1, 'message': 'Failed loading chat Chat#42 from database: Query returned no rows'}")


class DbCase(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = database.DB_PATH
        database.DB_PATH = TEST_DB
        database.init_db()
        bot._rate_limits.clear()

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        bot._rate_limits.clear()
        for f in (TEST_DB, f"{TEST_DB}-wal", f"{TEST_DB}-shm"):
            if os.path.exists(f):
                os.remove(f)


class TestChannelDatabase(DbCase):
    def test_add_channel_subscribes_its_chat(self):
        self.assertTrue(database.add_channel(42, "alerts", "Alerts", "7", 1, "https://i.delta.chat/#a"))
        self.assertEqual(database.get_subscribers("alerts"), [42])
        ch = database.get_channel_by_topic("alerts")
        self.assertEqual((ch["chat_id"], ch["name"], ch["owner"], ch["acc_id"], ch["invite"]),
                         (42, "Alerts", "7", 1, "https://i.delta.chat/#a"))

    def test_topic_unique(self):
        database.add_channel(42, "alerts", "Alerts", "7", 1, "x")
        self.assertFalse(database.add_channel(43, "alerts", "Other", "8", 1, "y"))
        self.assertEqual(database.get_subscribers("alerts"), [42])
        self.assertIsNone(database.get_channel_by_topic("missing"))

    def test_topic_in_use(self):
        self.assertFalse(database.topic_in_use("free"))
        database.subscribe(5, "subbed")
        database.add_channel(42, "chan", "Chan", "7", 1, "x")
        self.assertTrue(database.topic_in_use("subbed"))
        self.assertTrue(database.topic_in_use("chan"))

    def test_owner_queries(self):
        database.add_channel(42, "a1", "A1", "7", 1, "x")
        database.add_channel(43, "a2", "A2", "7", 1, "x")
        database.add_channel(44, "b1", "B1", "8", 1, "x")
        self.assertEqual([c["topic"] for c in database.get_channels_by_owner("7")], ["a1", "a2"])
        self.assertEqual(database.count_channels_by_owner("7"), 2)
        self.assertEqual(database.count_channels_by_owner("9"), 0)

    def test_delete_channel(self):
        database.add_channel(42, "alerts", "Alerts", "7", 1, "x")
        database.subscribe(5, "alerts")
        self.assertEqual(database.delete_channel("alerts")["chat_id"], 42)
        self.assertIsNone(database.get_channel_by_topic("alerts"))
        self.assertEqual(database.get_subscribers("alerts"), [5])
        self.assertIsNone(database.delete_channel("alerts"))

    def test_drop_gone_channels_only_touches_channels(self):
        database.add_channel(42, "alerts", "Alerts", "7", 1, "x")
        database.subscribe(5, "alerts")
        self.assertEqual(database.drop_gone_channels([42, 5]), 1)
        self.assertIsNone(database.get_channel_by_topic("alerts"))
        self.assertEqual(database.get_subscribers("alerts"), [5])
        self.assertEqual(database.drop_gone_channels([]), 0)


class TestParseAndTopic(DbCase):
    def test_parse(self):
        self.assertEqual(bot.parse_newchannel_args('"My Alerts"'), ("My Alerts", None))
        self.assertEqual(bot.parse_newchannel_args('"My Alerts" alerts'), ("My Alerts", "alerts"))
        self.assertEqual(bot.parse_newchannel_args('alerts'), ("alerts", None))
        self.assertEqual(bot.parse_newchannel_args("'Ops team' ops-1"), ("Ops team", "ops-1"))

    def test_parse_bad(self):
        for payload in ("", "   ", '"unclosed', 'a b c', '""', '"' + "x" * 65 + '"'):
            self.assertIsNone(bot.parse_newchannel_args(payload), payload)

    def test_random_topic(self):
        a, b = bot.random_channel_topic(), bot.random_channel_topic()
        self.assertRegex(a, r"^[A-Za-z0-9]{12}$")
        self.assertTrue(bot.valid_topic(a))
        self.assertNotEqual(a, b)

    def test_explicit_topic(self):
        self.assertEqual(bot.choose_channel_topic("My Alerts", "alerts"), ("alerts", None))

    def test_explicit_topic_invalid_or_taken(self):
        database.subscribe(5, "subbed")
        database.add_channel(42, "chan", "Chan", "7", 1, "x")
        for topic in ("bad topic", "subbed", "chan"):
            chosen, error = bot.choose_channel_topic("Name", topic)
            self.assertIsNone(chosen)
            self.assertTrue(error.startswith("❌"))

    def test_topic_from_name(self):
        self.assertEqual(bot.choose_channel_topic("alerts", None), ("alerts", None))

    def test_random_when_name_taken_or_not_a_topic(self):
        database.subscribe(5, "alerts")
        for name in ("alerts", "My Alerts", "x" * 64 + "y"):
            chosen, error = bot.choose_channel_topic(name, None)
            self.assertIsNone(error)
            self.assertRegex(chosen, r"^[A-Za-z0-9]{12}$")


def make_event(payload, from_id=7, chat_id=100, text=None):
    msg = SimpleNamespace(chat_id=chat_id, from_id=from_id, id=555, text=text or "/x")
    return SimpleNamespace(msg=msg, payload=payload)


def make_bot(chat_type="Single"):
    b = MagicMock()
    b.rpc.get_basic_chat_info.return_value = {"chat_type": chat_type}
    b.rpc.create_broadcast.return_value = 42
    b.rpc.get_chat_securejoin_qr_code.return_value = "https://i.delta.chat/#INVITE"
    return b


def reply_text(b):
    return b.rpc.send_msg.call_args[0][2].text


class TestNewChannelCommand(DbCase):
    def setUp(self):
        super().setUp()
        p = patch("bot._is_dc_admin", return_value=False)
        self.is_admin = p.start()
        self.addCleanup(p.stop)

    def test_create_with_random_topic(self):
        b = make_bot()
        bot.newchannel_command(b, 1, make_event('"My Alerts"'))
        b.rpc.create_broadcast.assert_called_once_with(1, "My Alerts")
        b.rpc.get_chat_securejoin_qr_code.assert_called_once_with(1, 42)
        ch = database.get_channels_by_owner("7")[0]
        self.assertRegex(ch["topic"], r"^[A-Za-z0-9]{12}$")
        self.assertEqual(database.get_subscribers(ch["topic"]), [42])
        text = reply_text(b)
        for part in ("https://i.delta.chat/#INVITE", ch["topic"], f"/send {ch['topic']} **title** message",
                     f"/{ch['topic']}", "password"):
            self.assertIn(part, text)

    def test_create_with_explicit_topic(self):
        b = make_bot()
        bot.newchannel_command(b, 1, make_event('"My Alerts" myalerts'))
        self.assertEqual(database.get_channel_by_topic("myalerts")["chat_id"], 42)

    def test_refused_in_group(self):
        b = make_bot("Group")
        bot.newchannel_command(b, 1, make_event('"My Alerts"'))
        b.rpc.create_broadcast.assert_not_called()
        self.assertIn("private chat", reply_text(b))

    def test_usage_on_bad_input(self):
        b = make_bot()
        bot.newchannel_command(b, 1, make_event('"unclosed'))
        b.rpc.create_broadcast.assert_not_called()
        self.assertIn("Usage", reply_text(b))

    def test_refused_for_taken_explicit_topic(self):
        database.subscribe(5, "taken")
        b = make_bot()
        bot.newchannel_command(b, 1, make_event('"X" taken'))
        b.rpc.create_broadcast.assert_not_called()
        self.assertIn("already in use", reply_text(b))

    def test_limit_per_user(self):
        for i in range(bot.CHANNELS_MAX_PER_USER):
            database.add_channel(1000 + i, f"t{i}", "T", "7", 1, "x")
        b = make_bot()
        bot.newchannel_command(b, 1, make_event('"One more"'))
        b.rpc.create_broadcast.assert_not_called()
        self.assertIn(str(bot.CHANNELS_MAX_PER_USER), reply_text(b))

    def test_admin_has_no_limit(self):
        for i in range(bot.CHANNELS_MAX_PER_USER):
            database.add_channel(1000 + i, f"t{i}", "T", "7", 1, "x")
        self.is_admin.return_value = True
        b = make_bot()
        bot.newchannel_command(b, 1, make_event('"One more"'))
        b.rpc.create_broadcast.assert_called_once()
        self.assertEqual(database.count_channels_by_owner("7"), bot.CHANNELS_MAX_PER_USER + 1)

    def test_creation_rate_limited(self):
        b = make_bot()
        for i in range(bot.NEWCHANNEL_RATE_MAX + 1):
            b.rpc.create_broadcast.return_value = 42 + i
            bot.newchannel_command(b, 1, make_event(f'"C{i}"'))
        self.assertEqual(b.rpc.create_broadcast.call_count, bot.NEWCHANNEL_RATE_MAX)
        self.assertIn("Too many", reply_text(b))

    def test_rpc_error_leaves_no_row(self):
        b = make_bot()
        b.rpc.create_broadcast.side_effect = Exception("boom")
        bot.newchannel_command(b, 1, make_event('"My Alerts"'))
        self.assertEqual(database.count_channels_by_owner("7"), 0)
        b.rpc.delete_chat.assert_not_called()
        self.assertIn("Failed", reply_text(b))

    def test_invite_error_deletes_chat(self):
        b = make_bot()
        b.rpc.get_chat_securejoin_qr_code.side_effect = Exception("boom")
        bot.newchannel_command(b, 1, make_event('"My Alerts"'))
        b.rpc.delete_chat.assert_called_once_with(1, 42)
        self.assertEqual(database.count_channels_by_owner("7"), 0)

    def test_db_error_deletes_chat(self):
        b = make_bot()
        with patch("bot.database.add_channel", return_value=False):
            bot.newchannel_command(b, 1, make_event('"My Alerts"'))
        b.rpc.delete_chat.assert_called_once_with(1, 42)
        self.assertIn("Failed", reply_text(b))

    def test_load_bound_per_creation(self):
        """One creation: at most 4 RPC calls and 4 DB connections, whatever the data size."""
        for i in range(500):  # other users' channels and subscriptions must not add work
            database.add_channel(5000 + i, f"o{i}", "O", str(i + 100), 1, "x")
            database.subscribe(9000 + i, f"s{i}")
        b = make_bot()
        self.is_admin.side_effect = AssertionError("no admin check below the limit")
        real_connect = database._connect
        connects = []

        def counting_connect(*a, **k):
            connects.append(1)
            return real_connect(*a, **k)

        with patch("database._connect", side_effect=counting_connect):
            bot.newchannel_command(b, 1, make_event('"My Alerts"'))
        rpc_calls = [c[0] for c in b.rpc.method_calls]
        self.assertEqual(b.rpc.create_broadcast.call_count, 1)
        self.assertLessEqual(len(rpc_calls), 4, rpc_calls)
        self.assertLessEqual(len(connects), 4)


class TestChannelManagement(DbCase):
    def setUp(self):
        super().setUp()
        p = patch("bot._is_dc_admin", return_value=False)
        self.is_admin = p.start()
        self.addCleanup(p.stop)
        database.add_channel(42, "mine", "Mine", "7", 1, "https://i.delta.chat/#MINE")
        database.add_channel(43, "theirs", "Theirs", "8", 2, "https://i.delta.chat/#THEIRS")

    def test_list_shows_only_own(self):
        b = make_bot()
        bot.ntfychannels_command(b, 1, make_event(""))
        text = reply_text(b)
        self.assertIn("mine", text)
        self.assertIn("https://i.delta.chat/#MINE", text)
        self.assertNotIn("theirs", text)
        self.assertNotIn("#THEIRS", text)

    def test_list_empty(self):
        b = make_bot()
        bot.ntfychannels_command(b, 1, make_event("", from_id=9))
        self.assertIn("no channels", reply_text(b))

    def test_list_refused_in_group(self):
        b = make_bot("Group")
        bot.ntfychannels_command(b, 1, make_event(""))
        self.assertNotIn("#MINE", reply_text(b))

    def test_owner_deletes_own(self):
        b = make_bot()
        bot.ntfychanneldel_command(b, 1, make_event("mine"))
        self.assertIsNone(database.get_channel_by_topic("mine"))
        self.assertEqual(database.get_subscribers("mine"), [])
        b.rpc.delete_chat.assert_called_once_with(1, 42)

    def test_admin_deletes_any(self):
        self.is_admin.return_value = True
        b = make_bot()
        bot.ntfychanneldel_command(b, 1, make_event("theirs"))
        self.assertIsNone(database.get_channel_by_topic("theirs"))
        b.rpc.delete_chat.assert_called_once_with(2, 43)  # on the account that created it

    def test_other_user_refused(self):
        b = make_bot()
        bot.ntfychanneldel_command(b, 1, make_event("theirs"))
        self.assertIsNotNone(database.get_channel_by_topic("theirs"))
        b.rpc.delete_chat.assert_not_called()
        self.assertIn("not your channel", reply_text(b))

    def test_delete_unknown_and_usage(self):
        b = make_bot()
        bot.ntfychanneldel_command(b, 1, make_event("nope"))
        self.assertIn("No channel", reply_text(b))
        bot.ntfychanneldel_command(b, 1, make_event(""))
        self.assertIn("Usage", reply_text(b))

    def test_help_lists_channel_commands(self):
        b = make_bot()
        b.rpc.get_contact.return_value = SimpleNamespace(address="u@example.org")
        text = bot.get_help_text(b, 1, 7)
        for cmd in ("/newchannel", "/ntfychannels", "/ntfychanneldel"):
            self.assertIn(cmd, text)
        self.assertNotIn("/newgroup", text)


class TestChannelFanOut(DbCase):
    def setUp(self):
        super().setUp()
        self.saved = (bot.dc_bot_instance, bot.dc_accid)
        bot.dc_bot_instance = MagicMock()
        bot.dc_bot_instance.rpc.get_all_account_ids.return_value = [1]
        bot.dc_bot_instance.rpc.get_basic_chat_info.return_value = {"chat_type": "OutBroadcast"}
        bot.dc_accid = 1
        p = patch("bot.queue_notification")
        self.queue = p.start()
        self.addCleanup(p.stop)
        database.add_channel(42, "alerts", "Alerts", "7", 1, "x")

    def tearDown(self):
        bot.dc_bot_instance, bot.dc_accid = self.saved
        super().tearDown()

    def test_publish_queues_one_message_for_the_channel(self):
        bot._publish_from_chat(MagicMock(), 1, "alerts", "hello", sender_chat_id=100)
        self.assertEqual(self.queue.call_count, 1)
        self.assertEqual(self.queue.call_args[0][1], 42)

    def test_gone_channel_is_forgotten(self):
        database.subscribe(5, "alerts")  # a gone private chat: not a channel, left alone
        bot.dc_bot_instance.rpc.get_basic_chat_info.side_effect = GONE
        bot._publish_from_chat(MagicMock(), 1, "alerts", "hello", sender_chat_id=100)
        self.assertIsNone(database.get_channel_by_topic("alerts"))
        self.assertEqual(database.get_subscribers("alerts"), [5])

    def test_other_errors_keep_the_channel(self):
        bot.dc_bot_instance.rpc.get_basic_chat_info.side_effect = Exception("database is locked")
        bot._publish_from_chat(MagicMock(), 1, "alerts", "hello", sender_chat_id=100)
        self.assertIsNotNone(database.get_channel_by_topic("alerts"))

    def test_http_post_forgets_gone_channel(self):
        bot.dc_bot_instance.rpc.get_basic_chat_info.side_effect = GONE
        req = MagicMock(remote="203.0.113.9")
        req.headers = {}
        req.query = {}
        req.match_info = {"topic": "alerts"}
        req.path = "/alerts"
        req.read = AsyncMock(return_value=b"hello")
        with patch.object(bot, "AUTH_TOKEN", None):
            resp = asyncio.run(bot.handle_ntfy_post(req))
        self.assertEqual(resp.status, 200)
        self.assertIsNone(database.get_channel_by_topic("alerts"))


if __name__ == "__main__":
    unittest.main()
