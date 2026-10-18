"""The paced outbox: send order by notification priority, pacing, backlog warning - and that an
attachment's temp file lives until its last queued send."""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

TEST_DB = "test_ntfy_outbox.db"
os.environ.setdefault("DB_PATH", TEST_DB)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
import outbox
from outbox import Outbox, PRIO_URGENT, PRIO_REPLY, PRIO_NORMAL, PRIO_LOW


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def queued(burst=100, clock=None):
    """An outbox that queues (as if started) but has no worker: the test drains it by hand."""
    ob = Outbox(rate_per_minute=60, burst=burst, clock=clock or FakeClock())
    ob._thread = object()
    return ob


def drain(ob):
    sent = []
    while True:
        with ob._cond:
            while ob._heap and ob._heap[0][2].done:
                ob._heap.pop(0)
            if not ob._heap:
                return sent
            job = ob._next_job()
        ob._run(job)
        sent.append(job.label)


class TestOutbox(unittest.TestCase):
    def test_not_started_sends_inline(self):
        self.assertEqual(Outbox().submit(lambda: 7).result(timeout=0), 7)

    def test_order(self):
        ob = queued()
        for prio, label in ((PRIO_LOW, "low"), (PRIO_NORMAL, "normal 1"), (PRIO_REPLY, "reply"),
                            (PRIO_URGENT, "urgent"), (PRIO_NORMAL, "normal 2")):
            ob.submit(lambda: None, priority=prio, label=label)
        self.assertEqual(drain(ob), ["urgent", "reply", "normal 1", "normal 2", "low"])

    def test_paces_after_burst(self):
        ob = Outbox(rate_per_minute=600, burst=2)
        ob.start()
        t0 = time.monotonic()
        times = [f.result(timeout=5) for f in [ob.submit(time.monotonic) for _ in range(6)]]
        self.assertLess(times[1] - t0, 0.05)
        self.assertGreater(times[-1] - t0, 0.35)

    def test_backlog_is_logged_once_a_minute(self):
        clock = FakeClock()
        ob = queued(burst=1, clock=clock)
        for _ in range(outbox.BACKLOG_WARN + 1):
            ob.submit(lambda: None)
        with self.assertLogs("ntfy_bot", level="WARNING") as logs, ob._cond:
            ob._next_job()          # uses the only token
            ob._tokens = 0
            ob._warn_backlog()
            ob._warn_backlog()      # same minute: no second line
            clock.t += 61
            ob._warn_backlog()
        self.assertEqual(len(logs.output), 2)
        self.assertIn("messages waiting for the send limit", logs.output[0])

    def test_defaults_match_relay_headroom(self):
        self.assertLess(outbox.RATE_PER_MINUTE, 60)
        self.assertLess(outbox.BURST, 10)


class TestNotificationsQueued(unittest.TestCase):
    def setUp(self):
        self.ob = queued()
        self.saved = (bot.dc_bot_instance, bot.dc_accid)
        bot.dc_bot_instance = MagicMock()
        bot.dc_bot_instance.rpc.get_all_account_ids.return_value = [1]
        bot.dc_bot_instance.rpc.get_basic_chat_info.return_value = {"chat_type": "Group"}
        bot.dc_accid = 1
        patcher = patch.object(bot, "dc_outbox", self.ob)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        bot.dc_bot_instance, bot.dc_accid = self.saved

    def _post(self, raw, headers):
        req = MagicMock()
        req.match_info = {"topic": "alerts"}
        req.headers = {"X-Forwarded-For": "203.0.113.51", "Content-Type": "", **headers}
        req.query = {}
        req.path = "/alerts"
        req.read = AsyncMock(return_value=raw)
        with patch("bot.database.add_notification"), patch("bot.database.get_subscribers", return_value=[11, 12]):
            return asyncio.run(bot.handle_ntfy_post(req))

    def test_priority_mapping(self):
        self.assertEqual(bot.outbox_priority(5), PRIO_URGENT)
        self.assertEqual(bot.outbox_priority(4), PRIO_URGENT)
        self.assertEqual(bot.outbox_priority(3), PRIO_NORMAL)
        self.assertEqual(bot.outbox_priority(None), PRIO_NORMAL)
        self.assertEqual(bot.outbox_priority(1), PRIO_LOW)

    def test_urgent_notification_goes_before_replies(self):
        bot._dc_send_msg_with_stats(MagicMock(), 1, 99, MagicMock())  # a command reply waiting already
        resp = self._post(b"disk full", {"Priority": "5"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.ob.waiting(), {"urgent": 2, "reply": 1})
        self.assertEqual(drain(self.ob), ["#alerts to chat 11", "#alerts to chat 12", "message to chat 99"])
        self.assertEqual(bot.dc_bot_instance.rpc.send_msg.call_count, 2)

    def test_attachment_kept_until_last_send(self):
        queued_data = []
        real = bot.queue_notification

        def spy(acc_id, chat_id, msg_data, priority, topic):
            queued_data.append(msg_data)
            return real(acc_id, chat_id, msg_data, priority, topic)
        with patch.object(bot, "queue_notification", side_effect=spy):
            self._post(b"\x89PNG fake image", {"Filename": "graph.png"})
        file_path = queued_data[0].file
        # The HTTP request is done, the sends still wait: the file must still be there
        self.assertTrue(os.path.exists(file_path))
        drain(self.ob)
        sent = bot.dc_bot_instance.rpc.send_msg.call_args_list
        self.assertEqual([c.args[2].file for c in sent], [file_path, file_path])
        self.assertFalse(os.path.exists(file_path))  # removed after the second send

    def test_temp_file_removed_without_subscribers(self):
        import tempfile
        f = tempfile.NamedTemporaryFile(delete=False)
        f.close()
        bot.remove_file_when_sent(f.name, [])
        self.assertFalse(os.path.exists(f.name))


if __name__ == "__main__":
    unittest.main()
