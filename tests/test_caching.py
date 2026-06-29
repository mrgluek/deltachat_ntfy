import os
import sys
import unittest
from unittest.mock import MagicMock, patch

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

# Import the actual modules to be tested
import database
import bot

class TestCaching(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Refresh config database path
        database.DB_PATH = TEST_DB
        database.init_db()
        bot.clear_index_cache()

    def tearDown(self):
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except OSError:
                pass

    async def test_handle_index_caching(self):
        request = MagicMock()
        
        # Initially None
        self.assertIsNone(bot.index_page_html_cache)
        
        # First call: generates HTML and caches it
        response1 = await bot.handle_index(request)
        self.assertIsNotNone(bot.index_page_html_cache)
        html1 = response1.text
        self.assertIn("Delta Chat Ntfy Bot", html1)
        
        # Temporarily mock database.get_config to return something different
        # Since it is cached, it should still return the old config URL
        with patch('database.get_config', return_value="https://changed.url.com"):
            response2 = await bot.handle_index(request)
            html2 = response2.text
            self.assertEqual(html1, html2)
            self.assertNotIn("https://changed.url.com", html2)
            
        # Clear cache and call again: should reflect the new mock config
        bot.clear_index_cache()
        self.assertIsNone(bot.index_page_html_cache)
        
        with patch('database.get_config', return_value="https://changed.url.com"):
            response3 = await bot.handle_index(request)
            html3 = response3.text
            self.assertIn("https://changed.url.com", html3)

    async def test_handle_static_headers(self):
        request = MagicMock()
        request.path = '/icon.png'
        
        with patch('os.path.exists', return_value=True), \
             patch('aiohttp.web.FileResponse') as mock_file_response:
             
            await bot.handle_static(request)
            mock_file_response.assert_called_once_with('icon.png', headers={
                'Cache-Control': 'public, max-age=31536000, immutable'
            })
