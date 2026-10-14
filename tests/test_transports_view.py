import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import transports_view

HTML = (
    '<html><body><h3>Incoming messages</h3><ul>'
    '<li class="transport"><span class="green dot"></span> <b>chat.example.org:</b> Connected<br />'
    '<ul class="quota-list"><li>120 MiB of 2 GiB used<div class="bar"><div class="progress grey" style="width: 6%">6%</div></div></li></ul></li>'
    '<li class="transport"><span class="red dot"></span> <b>uk.example.org:</b> Error: connection timed out<br /></li>'
    '</ul><h3>Outgoing messages</h3><ul><li><span class="green dot"></span> Your last message was sent successfully.</li></ul>'
    '</body></html>'
)


class TestTransportsView(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db = tmp.name
        c = sqlite3.connect(self.db)
        c.executescript("""
            CREATE TABLE transports (id INTEGER PRIMARY KEY, addr TEXT, entered_param TEXT, configured_param TEXT,
                                     add_timestamp INTEGER, is_published INTEGER, last_rcvd_timestamp INTEGER);
            CREATE TABLE smtp_success (id INTEGER PRIMARY KEY, transport_id INTEGER UNIQUE);
            CREATE TABLE smtp2 (id INTEGER PRIMARY KEY, rfc724_mid TEXT, recipients TEXT, mime TEXT, msg_id INTEGER, retries INTEGER);
        """)
        now = int(time.time())
        c.execute("INSERT INTO transports VALUES (1, 'bot@chat.example.org', '', '', ?, 1, ?)", (now - 86400 * 20, now - 300))
        # Newer relay, but the core last sent through transport 1.
        c.execute("INSERT INTO transports VALUES (4, 'bot@uk.example.org', '', '', ?, 0, ?)", (now - 86400 * 10, now - 7200))
        c.execute("INSERT INTO smtp_success (id, transport_id) VALUES (496, 1)")
        c.execute("INSERT INTO smtp2 (retries) VALUES (2)")
        c.commit()
        c.close()
        self.rpc = MagicMock()
        self.rpc.get_info.return_value = {"database_dir": self.db}
        self.rpc.get_connectivity_html.return_value = HTML
        self.transports = [{"addr": "bot@chat.example.org"}, {"addr": "bot@uk.example.org"}]

    def tearDown(self):
        os.unlink(self.db)

    def test_report_from_core_state(self):
        text = transports_view.build_transports_report(self.rpc, 1, self.transports)
        self.assertIn("Sending via:** `bot@chat.example.org`", text)
        self.assertIn("1 message(s) waiting", text)
        self.assertIn("retried up to 2×", text)
        self.assertIn("120 MiB of 2 GiB used (6%)", text)
        self.assertIn("🔴 Error: connection timed out", text)
        self.assertIn("Your last message was sent successfully", text)
        self.assertIn("not published", text)
        self.assertLess(text.index("1. `bot@chat.example.org`"), text.index("2. `bot@uk.example.org`"))

    def test_fallback_without_core_db(self):
        self.rpc.get_info.return_value = {}
        text = transports_view.build_transports_report(self.rpc, 1, self.transports)
        self.assertIn("bot@uk.example.org", text)
        self.assertIn("could not read the core database", text)
        self.assertIn("🟢 Connected", text)

    def test_parse_connectivity_html(self):
        info = transports_view.parse_connectivity_html(HTML)
        self.assertEqual(len(info["relays"]), 2)
        self.assertEqual(info["relays"][0]["quota_pct"], 6)
        self.assertEqual(info["relays"][1]["color"], "red")
        self.assertEqual(info["outgoing"]["color"], "green")


if __name__ == "__main__":
    unittest.main()
