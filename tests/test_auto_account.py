"""
Tests for automatic account creation on `serve` (ensure_configured_account).
The core is mocked: no account is ever registered on a real relay.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot


class TestAutoAccount(unittest.TestCase):
    def _args(self, account=None):
        args = MagicMock(account=account)
        args.cmd = bot.dc_cli.__class__  # anything not named _serve_cmd
        return args

    def _serve_args(self, account=None):
        def _serve_cmd(*_):
            pass
        return MagicMock(account=account, cmd=_serve_cmd)

    def test_creates_account_on_serve_without_configured_one(self):
        b = MagicMock()
        b.rpc.get_all_account_ids.return_value = []
        b.rpc.add_account.return_value = 1
        b.rpc.get_contact.return_value = {"address": "abc@chatmail.example"}  # self contact
        bot.ensure_configured_account(b, self._serve_args())
        b.rpc.set_config.assert_any_call(1, "bot", "1")
        b.rpc.init_transports.assert_called_once_with(1, None)

    def test_reuses_unconfigured_account(self):
        b = MagicMock()
        b.rpc.get_all_account_ids.return_value = [3]
        b.rpc.is_configured.return_value = False
        bot.ensure_configured_account(b, self._serve_args())
        b.rpc.add_account.assert_not_called()
        b.rpc.init_transports.assert_called_once_with(3, None)

    def test_uses_account_given_with_option(self):
        b = MagicMock()
        b.rpc.is_configured.return_value = False
        bot.ensure_configured_account(b, self._serve_args(account=5))
        b.rpc.get_all_account_ids.assert_not_called()
        b.rpc.init_transports.assert_called_once_with(5, None)

    def test_nothing_when_configured_or_not_serving(self):
        b = MagicMock()
        b.rpc.get_all_account_ids.return_value = [1]
        b.rpc.is_configured.return_value = True
        bot.ensure_configured_account(b, self._serve_args())
        bot.ensure_configured_account(b, self._args())
        b.rpc.init_transports.assert_not_called()
        b.rpc.add_account.assert_not_called()

    def test_failure_is_logged_not_raised(self):
        b = MagicMock()
        b.rpc.get_all_account_ids.return_value = []
        b.rpc.init_transports.side_effect = RuntimeError("no relay answered")
        bot.ensure_configured_account(b, self._serve_args())
        b.logger.error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
