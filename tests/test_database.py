"""
Tests for database operations in deltachat_ntfy.
Adheres to AGENTS.md database unit testing conventions.
"""
import os
import time
import unittest
import sys

# Ensure root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database

TEST_DB_PATH = "test_ntfy_database.db"


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = database.DB_PATH
        database.DB_PATH = TEST_DB_PATH
        with database._transport_stats_lock:
            database._transport_stats_buffer.clear()
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        for f in (TEST_DB_PATH, f"{TEST_DB_PATH}-wal", f"{TEST_DB_PATH}-shm"):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

    # ── Config Tests ────────────────────────────────────────────────────────
    def test_config_set_get_roundtrip(self):
        database.set_config("test_key", "test_value")
        self.assertEqual(database.get_config("test_key"), "test_value")

    def test_config_overwrite(self):
        database.set_config("test_key", "v1")
        database.set_config("test_key", "v2")
        self.assertEqual(database.get_config("test_key"), "v2")

    def test_config_missing_key(self):
        self.assertIsNone(database.get_config("nonexistent_key_12345"))

    # ── Admin Fingerprint Tests ─────────────────────────────────────────────
    def test_admin_fingerprint_get_set(self):
        self.assertIsNone(database.get_admin_fingerprint())
        database.set_admin_fingerprint("A1B2C3D4E5F6")
        self.assertEqual(database.get_admin_fingerprint(), "A1B2C3D4E5F6")

    # ── Resilient Mode Flag Tests ───────────────────────────────────────────
    def test_resilient_flag_toggle(self):
        self.assertIsNone(database.get_config("resilient"))
        database.set_config("resilient", "1")
        self.assertEqual(database.get_config("resilient"), "1")
        database.set_config("resilient", "0")
        self.assertEqual(database.get_config("resilient"), "0")

    # ── Subscription Tests ──────────────────────────────────────────────────
    def test_subscriptions(self):
        self.assertNotIn("alerts", database.get_subscriptions(100))
        self.assertTrue(database.subscribe(100, "alerts"))
        self.assertIn("alerts", database.get_subscriptions(100))

        # Duplicate subscribe returns False
        self.assertFalse(database.subscribe(100, "alerts"))

        database.subscribe(100, "news")
        database.subscribe(200, "alerts")

        subs = database.get_subscriptions(100)
        self.assertIn("alerts", subs)
        self.assertIn("news", subs)

        subscribers = database.get_subscribers("alerts")
        self.assertIn(100, subscribers)
        self.assertIn(200, subscribers)

        self.assertTrue(database.unsubscribe(100, "alerts"))
        self.assertNotIn("alerts", database.get_subscriptions(100))
        self.assertFalse(database.unsubscribe(100, "alerts"))

    # ── Notification Tests ──────────────────────────────────────────────────
    def test_notifications_and_retrieval(self):
        self.assertEqual(database.get_notifications_last_24h(), 0)

        database.add_notification(
            topic="server",
            title="CPU High",
            message="Usage > 90%",
            priority=4
        )
        self.assertEqual(database.get_notifications_last_24h(), 1)

        recent = database.get_recent_notifications(["server"], limit=10)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["title"], "CPU High")
        self.assertEqual(recent[0]["priority"], 4)

        since = database.get_messages_since("server", since="1h")
        self.assertEqual(len(since), 1)

    def test_cleanup_old_records(self):
        database.add_notification(
            topic="test",
            title="Old Notification",
            message="Old message",
            priority=3
        )
        self.assertEqual(database.get_notifications_last_24h(), 1)

        # Cleanup with 0 retention days deletes everything older than right now
        # But wait 1s to ensure created_at < cutoff
        time.sleep(1.1)
        res = database.cleanup_old_records(retention_days=0)
        self.assertGreaterEqual(res["notifications"], 1)
        self.assertEqual(database.get_notifications_last_24h(), 0)

    # ── Transport Statistics Tests (Buffered) ───────────────────────────────
    def test_transport_stats_accumulation_and_flush(self):
        addr1 = "bot1@example.com"
        addr2 = "bot2@example.com"

        database.increment_transport_sent(addr1)
        database.increment_transport_sent(addr1)
        database.increment_transport_received(addr1)

        database.increment_transport_sent(addr2)
        database.increment_transport_received(addr2)
        database.increment_transport_received(addr2)

        # Flush buffer to SQLite
        database.flush_transport_stats()

        stats = database.get_all_transport_stats()
        stats_dict = {s["addr"]: s for s in stats}

        self.assertIn(addr1, stats_dict)
        self.assertIn(addr2, stats_dict)

        self.assertEqual(stats_dict[addr1]["msgs_sent"], 2)
        self.assertEqual(stats_dict[addr1]["msgs_received"], 1)
        self.assertIsNotNone(stats_dict[addr1]["last_sent_at"])
        self.assertIsNotNone(stats_dict[addr1]["last_received_at"])

        self.assertEqual(stats_dict[addr2]["msgs_sent"], 1)
        self.assertEqual(stats_dict[addr2]["msgs_received"], 2)

    def test_invalid_transport_addrs_ignored(self):
        database.increment_transport_sent("")
        database.increment_transport_sent("no-at-sign")
        database.increment_transport_received(None)
        database.flush_transport_stats()
        self.assertEqual(len(database.get_all_transport_stats()), 0)


if __name__ == "__main__":
    unittest.main()
