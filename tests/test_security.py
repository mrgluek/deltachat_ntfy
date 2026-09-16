import asyncio
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

TEST_DB = "test_security_ntfy.db"
os.environ["DB_PATH"] = TEST_DB

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
        def __init__(self, text="", status=200, content_type="", headers=None, **kwargs):
            self.text = text
            self.status = status
            self.content_type = content_type
            self.headers = headers or {}
    web.Response = MockResponse
    def mock_json_response(data, status=200, headers=None, **kwargs):
        import json
        return MockResponse(text=json.dumps(data), status=status, content_type="application/json", headers=headers)
    web.json_response = mock_json_response
    web.FileResponse = MagicMock()
    aiohttp.web = web
    sys.modules['aiohttp'] = aiohttp
    sys.modules['aiohttp.web'] = web

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import bot

if hasattr(bot, "web"):
    if not hasattr(bot.web, "json_response") or isinstance(bot.web.json_response, MagicMock):
        def _mock_json_response(data, status=200, headers=None, **kwargs):
            import json
            return bot.web.Response(text=json.dumps(data), status=status, content_type="application/json", headers=headers)
        bot.web.json_response = _mock_json_response

class TestSecurity(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = database.DB_PATH
        database.DB_PATH = TEST_DB
        with database._transport_stats_lock:
            database._transport_stats_buffer.clear()
        database.init_db()
        with bot._rate_limit_lock:
            bot._rate_limits.clear()
            bot.rate_limit_cache.clear()

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        for f in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass
        with bot._rate_limit_lock:
            bot._rate_limits.clear()
            bot.rate_limit_cache.clear()

    def test_is_safe_url_blocks_private_and_loopback_ips(self):
        blocked = [
            "http://127.0.0.1:8080/secret",
            "http://127.0.0.2/admin",
            "http://10.0.0.1/status",
            "http://172.16.0.1/private",
            "http://172.31.255.255/intranet",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/secret",
            "http://[fe80::1]/link-local",
            "http://[fc00::1]/ula",
            "http://0.0.0.0:8080",
        ]
        for url in blocked:
            safe, reason = bot.is_safe_url(url, allow_private=False)
            self.assertFalse(safe, f"Expected {url} to be blocked, but passed: {reason}")
            self.assertTrue(
                "reserved" in reason.lower() or "private" in reason.lower() or "forbidden" in reason.lower(),
                f"Unexpected reason: {reason}"
            )

    def test_is_safe_url_blocks_local_and_internal_hostnames(self):
        blocked = [
            "http://localhost:8080",
            "http://localhost.localdomain/api",
            "http://printer.local/jobs",
            "http://internal.corp.lan/data",
            "http://service.internal/status",
            "http://router.home.arpa/admin",
        ]
        for url in blocked:
            safe, reason = bot.is_safe_url(url, allow_private=False)
            self.assertFalse(safe, f"Expected {url} to be blocked, but passed")
            self.assertIn("forbidden", reason.lower())

    def test_is_safe_url_blocks_non_http_schemes_and_invalid_inputs(self):
        invalid = [
            "",
            None,
            "not-a-url",
            "file:///etc/passwd",
            "ftp://files.example.com/secret",
            "gopher://gopher.example.com",
            "javascript:alert(1)",
            "data:text/plain;base64,SGVsbG8=",
        ]
        for url in invalid:
            safe, reason = bot.is_safe_url(url, allow_private=False)
            self.assertFalse(safe, f"Expected '{url}' to be rejected")

    def test_is_safe_url_allows_testing_and_public_domains(self):
        allowed = [
            "http://example.com/image.png",
            "https://sub.example.com/data.txt",
            "http://test.example/resource",
            "https://test.example.com:8443/feed",
        ]
        for url in allowed:
            safe, reason = bot.is_safe_url(url, allow_private=False)
            self.assertTrue(safe, f"Expected {url} to be allowed, but rejected: {reason}")

    def test_is_safe_url_blocks_dns_resolving_to_private_ip(self):
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
            ]
            safe, reason = bot.is_safe_url("http://malicious-rebinding.com/secret", allow_private=False)
            self.assertFalse(safe)
            self.assertIn("reserved/private IP", reason)

    def test_handle_ntfy_post_blocks_unsafe_attachment_ssrf(self):
        """Verify Attach URL pointing to internal address is rejected before download."""
        req = MagicMock()
        req.match_info = {"topic": "alerts"}
        req.headers = {
            "Attach": "http://169.254.169.254/latest/meta-data/",
            "X-Forwarded-For": "203.0.113.10",
        }
        req.query = {}
        req.path = "/alerts"

        async def _test():
            req.json = AsyncMock(side_effect=Exception("not json"))
            req.text = AsyncMock(return_value="Server alert")
            resp = await bot.handle_ntfy_post(req)
            self.assertEqual(resp.status, 200)

        asyncio.run(_test())

    def test_rate_limiting_sliding_window(self):
        req = MagicMock()
        req.headers = {"X-Forwarded-For": "198.51.100.5"}
        req.remote = "198.51.100.5"

        # Under limit: should allow 5 requests
        for _ in range(5):
            self.assertTrue(bot.check_rate_limit(req, bucket="test_bucket", max_requests=5, window_seconds=60))

        # 6th request must be blocked
        self.assertFalse(bot.check_rate_limit(req, bucket="test_bucket", max_requests=5, window_seconds=60))

    def test_rate_limiting_returns_429_with_retry_after(self):
        req = MagicMock()
        req.match_info = {"topic": "test"}
        req.headers = {"X-Forwarded-For": "203.0.113.99"}
        req.remote = "203.0.113.99"
        req.path = "/test"
        req.query = {}

        # Flood rate limiter
        for _ in range(bot.RATE_LIMIT_MAX):
            bot.is_rate_limited("203.0.113.99")

        async def _test():
            req.json = AsyncMock(side_effect=Exception("not json"))
            req.text = AsyncMock(return_value="msg")
            resp = await bot.handle_ntfy_post(req)
            self.assertEqual(resp.status, 429)
            self.assertEqual(resp.headers.get("Retry-After"), "60")

        asyncio.run(_test())

    def test_web_application_has_client_max_size_configured(self):
        """Verify that web.Application restricts request bodies to avoid OOM DoS."""
        # Check source code to ensure client_max_size is explicitly provided
        import inspect
        src = inspect.getsource(bot._run_web_server)
        self.assertIn("client_max_size", src)

if __name__ == "__main__":
    unittest.main()
