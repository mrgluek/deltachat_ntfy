"""Regression coverage for the 1.6.7 security fixes."""
import asyncio
import fnmatch
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot

ROOT = Path(__file__).resolve().parents[1]


def request(topic="alerts", headers=None, query=None, body=b"hello"):
    req = MagicMock()
    req.match_info = {"topic": topic} if topic is not None else {}
    req.headers = headers or {}
    req.query = query or {}
    req.path = "/" + (topic or "")
    req.remote = "203.0.113.100"
    req.read = AsyncMock(return_value=body)
    return req


class TestAttachmentPaths(unittest.TestCase):
    def test_encoded_paths_become_basenames_inside_temp_directory(self):
        for name, expected in (("%2fapp%2fdata%2fntfy.db", "ntfy.db"),
                               ("a%2f..%2f..%2fx", "x"),
                               ("..%2f..%2fx", "x"), ("%5cwin%5cx", "x")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                self.assertEqual(bot.safe_attachment_name(name), expected)
                with patch("bot.tempfile.mkdtemp", return_value=directory):
                    with bot.create_attachment_file(name) as attachment:
                        self.assertEqual(Path(attachment.name), Path(directory).resolve() / expected)

    def test_separator_free_even_in_default_extension(self):
        name = bot.safe_attachment_name("..", "/../\\x")
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)

    def test_containment_guard_if_sanitizer_returns_path(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("bot.tempfile.mkdtemp", return_value=directory), \
                patch("bot.safe_attachment_name", return_value="../escape"):
            with bot.create_attachment_file("ignored") as attachment:
                self.assertEqual(Path(attachment.name), Path(directory) / "attachment")


class TestAdminAuthorization(unittest.TestCase):
    def test_fingerprint_required_when_configured(self):
        b = MagicMock()
        b.rpc.get_contact.return_value.address = "admin@example.com"
        for admin_fp, contact_fp, expected in (("ABC", None, False), ("ABC", "abc", True),
                                                ("ABC", "DEF", False), (None, None, True)):
            with self.subTest(admin_fp=admin_fp, contact_fp=contact_fp), \
                    patch("bot.database.get_admin_fingerprint", return_value=admin_fp), \
                    patch("bot.database.get_config", return_value="ADMIN@example.com"), \
                    patch("bot._get_contact_fingerprint", return_value=contact_fp):
                self.assertEqual(bot._is_dc_admin(b, 1, 2), expected)

    def test_accounts_command_uses_shared_admin_check(self):
        b, event = MagicMock(), MagicMock()
        with patch("bot._is_dc_admin", return_value=False) as admin_check, \
                patch("bot._dc_send_msg_with_stats"):
            bot.accounts_command(b, 1, event)
        admin_check.assert_called_once_with(b, 1, event.msg.from_id)
        b.rpc.get_all_account_ids.assert_not_called()


class TestWebSecurity(unittest.TestCase):
    def setUp(self):
        self.patches = [patch("bot.is_rate_limited", return_value=False),
                        patch("bot.check_rate_limit", return_value=True),
                        patch("bot.AUTH_TOKEN", None),
                        patch("bot.database.add_notification"),
                        patch("bot.database.get_subscribers", return_value=[]),
                        patch("bot.push_to_listeners")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_invalid_topics_rejected_by_all_http_handlers(self):
        for topic in ("<script>alert(1)</script>", 'x" onclick="bad', "a/b", "a\\b", "a b", "a" * 65):
            for handler, status in ((bot.handle_topic_view, 404), (bot.handle_ntfy_json, 404),
                                    (bot.handle_ntfy_post, 400)):
                with self.subTest(topic=topic, handler=handler.__name__):
                    self.assertEqual(asyncio.run(handler(request(topic))).status, status)

    def test_json_and_header_topics_rejected(self):
        for req in (request(None, {"Topic": "<script>"}),
                    request(None, {"Content-Type": "application/json"},
                            body=b'{"topic":"<script>","message":"x"}')):
            self.assertEqual(asyncio.run(bot.handle_ntfy_post(req)).status, 400)

    def test_topic_charset_shared_with_commands(self):
        self.assertTrue(bot.valid_topic("Az09_.-"))
        self.assertTrue(bot.valid_topic("x" * 64))
        b, event = MagicMock(), MagicMock(payload="<script>")
        with patch("bot.database.subscribe") as subscribe, patch("bot._dc_send_msg_with_stats"):
            bot.sub_command(b, 1, event)
            subscribe.assert_not_called()

    def page(self):
        with patch("bot.database.get_config", return_value="https://ntfy.example"), \
                patch("bot.database.get_recent_notifications", return_value=[{
                    "id": 1, "created_at": 1, "priority": 3,
                    "title": "<script>alert(1)</script>", "message": "<script>bad</script>"
                }]), patch("bot.dc_bot_instance", None):
            return asyncio.run(bot.handle_topic_view(request())).text

    def test_server_render_escapes_notification_text(self):
        page = self.page()
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn('const topic = "alerts";', page)

    @unittest.skipUnless(shutil.which("node"), "Node.js needed to execute page JavaScript")
    def test_live_render_escapes_text_id_and_priority(self):
        page = self.page()
        functions = page[page.index("function escapeHtml"):page.index("function getPriorityEmoji")]
        script = '''
const seenIds = new Set(); let sinceParam = 0;
let result;
const card = {querySelector: () => ({addEventListener: () => {}})};
const document = {
 createElement: () => card,
 querySelector: selector => selector === '.notification-list' ? {insertBefore: c => result = c.innerHTML} : null
};
const getPriorityEmoji = () => 'x';
''' + functions + '''
const payload = `<script>bad</script>&"'`;
handleLiveMessage({id: payload, title: payload, message: payload + ' https://example.com', priority: payload, time: 1});
console.log(JSON.stringify({html: result, escaped: escapeHtml(payload)}));
'''
        result = subprocess.run([shutil.which("node"), "-e", script], check=True, capture_output=True, text=True)
        output = json.loads(result.stdout)
        self.assertNotIn("<script>", output["html"])
        self.assertNotIn('onclick=', output["html"])
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;&amp;&quot;&#39;', output["html"])
        self.assertIn('<a href="https://example.com"', output["html"])

    def test_tokens_from_all_three_sources_and_no_header_logging(self):
        for headers, query in (({"Authorization": "Bearer secret"}, {}),
                               ({"X-Auth-Token": "secret"}, {}), ({}, {"auth": "secret"}),
                               ({"Authorization": "Bearer wrong"}, {})):
            with self.subTest(headers=headers, query=query), patch("bot.AUTH_TOKEN", "secret"), \
                    patch("bot.hmac.compare_digest", wraps=bot.hmac.compare_digest) as compare, \
                    patch("bot.logger") as logger:
                resp = asyncio.run(bot.handle_ntfy_post(request(headers=headers, query=query)))
                self.assertEqual(resp.status, 401 if "wrong" in str(headers) else 200)
                self.assertTrue(compare.called)
                self.assertNotIn("secret", str(logger.mock_calls))
                self.assertNotIn("Authorization", str(logger.mock_calls))

    def test_attachment_dns_runs_off_event_loop_and_redirects_disabled(self):
        main_thread = threading.get_ident()
        threads = []
        def resolve(*args, **kwargs):
            threads.append(threading.get_ident())
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        response = MagicMock(status=302)
        response.headers = {"Location": "http://127.0.0.1/secret"}
        session = MagicMock()
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=session)
        with patch("bot.socket.getaddrinfo", side_effect=resolve), \
                patch("bot.database.get_config", return_value="0"), \
                patch.dict(os.environ, {"ALLOW_PRIVATE_NETWORKS": "0"}), \
                patch("bot.aiohttp.ClientSession", return_value=client):
            resp = asyncio.run(bot.handle_ntfy_post(request(headers={"Attach": "https://remote.social/file"})))
        self.assertEqual(resp.status, 200)
        self.assertTrue(threads)
        self.assertTrue(all(t != main_thread for t in threads))
        session.get.assert_called_once_with("https://remote.social/file", allow_redirects=False)

    def test_former_dns_bypasses_and_shared_address_space_rejected(self):
        for host in ("remote.social", "test.example", "example.com", "a.test"):
            with self.subTest(host=host), patch("bot.socket.getaddrinfo", return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 80))]) as dns:
                self.assertFalse(bot.is_safe_url("https://" + host, allow_private=False)[0])
                dns.assert_called_once()
        for host in ("100.64.0.1", "224.0.0.1", "[ff02::1]"):
            self.assertFalse(bot.is_safe_url("http://" + host, allow_private=False)[0])


class TestDeploymentSecurity(unittest.TestCase):
    def test_docker_build_excludes_secrets_and_development_files(self):
        patterns = (ROOT / ".dockerignore").read_text().splitlines()
        for name in (".env", ".env.local", ".env.production", "tests/", ".git", "README.md", "AGENTS.md"):
            self.assertTrue(any(fnmatch.fnmatch(name, p) for p in patterns), name)
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("install -y git", dockerfile)
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("no-new-privileges:true", compose)
        self.assertNotRegex(compose, r"(?m)^version:")

    def test_release_workflow_keeps_untrusted_values_out_of_shell_source(self):
        release = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertNotIn("curlimages/curl", release)
        self.assertIn('jq -n --arg version "$RELEASE_TAG"', release)
        # Expressions belong in env mappings, never in a run block.
        for line in release.splitlines():
            if '${{ github.ref_name }}' in line or '${{ github.actor }}' in line:
                self.assertRegex(line, r"^          RELEASE_(TAG|ACTOR):")
        tests = (ROOT / ".github/workflows/tests.yml").read_text()
        self.assertRegex(tests, r"(?m)^permissions:\n  contents: read$")

    @unittest.skipUnless(shutil.which("jq"), "jq needed to execute release payload construction")
    def test_release_payload_preserves_hostile_tag_as_data(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        script = workflow[workflow.index('          if [ -n "$SYNC_TOKEN" ]; then'):]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            capture = directory / "args.json"
            marker = directory / "injected"
            curl = directory / "curl"
            curl.write_text("#!" + sys.executable + "\nimport json, os, sys\n"
                            "with open(os.environ['CAPTURE'], 'w') as f: json.dump(sys.argv[1:], f)\n")
            curl.chmod(0o755)
            tag = 'v1.6.7";$(touch ' + str(marker) + ');\\tail'
            subprocess.run(["bash", "-c", script], check=True, capture_output=True,
                           env={**os.environ, "PATH": str(directory) + os.pathsep + os.environ["PATH"],
                                "SYNC_TOKEN": "test-token", "RELEASE_TAG": tag, "CAPTURE": str(capture)})
            args = json.loads(capture.read_text())
            payload = json.loads(args[args.index("-d") + 1])
            self.assertEqual(payload["client_payload"]["version"], tag)
            self.assertFalse(marker.exists())

    def test_forced_update_resets_selected_tag_before_compose(self):
        # Run a copied script against fake tools; no repository or Docker mutations.
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            script = directory / "update.sh"
            shutil.copy2(ROOT / "update.sh", script)
            (directory / ".env.local").write_text("UPDATE_REF=v1.6.6\n")
            git = directory / "git"
            git.write_text('''#!/bin/bash
printf 'git %s\\n' "$*" >> "$CALL_LOG"
case "$*" in
 'rev-parse refs/tags/v1.6.6^{commit}') echo chosen-release;;
 'rev-parse HEAD') echo local-head;;
 'describe --tags --always') echo v1.6.6;;
esac
''')
            docker = directory / "docker"
            docker.write_text('#!/bin/bash\nprintf "docker %s\\n" "$*" >> "$CALL_LOG"\n')
            git.chmod(0o755)
            docker.chmod(0o755)
            for flag in ("--force", "-f", ""):
                log = directory / "calls"
                log.write_text("")
                subprocess.run(["bash", str(script)] + ([flag] if flag else []), check=True,
                               capture_output=True, env={**os.environ, "PATH": str(directory) + os.pathsep + os.environ["PATH"],
                                                         "CALL_LOG": str(log), "MONITOR_URL": ""})
                calls = log.read_text().splitlines()
                self.assertLess(calls.index("git reset --hard chosen-release"), calls.index("docker compose up -d --build"))
        self.assertTrue(os.stat(ROOT / "update.sh").st_mode & 0o111)
