import importlib
import sys
import types
import unittest
from unittest.mock import patch


if "curl_cffi" not in sys.modules:
    curl_cffi = types.ModuleType("curl_cffi")
    curl_cffi.requests = types.SimpleNamespace()
    sys.modules["curl_cffi"] = curl_cffi

check = importlib.import_module("check")


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {"statusCode": 0, "data": []}
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


class GlobalBlockTests(unittest.TestCase):
    def setUp(self):
        check._LAST_API_REQUEST_AT = 0.0
        self.state = check.default_state()

    def test_403_stops_immediate_retries_and_starts_global_cooldown(self):
        session = FakeSession([FakeResponse(403)])

        with patch.object(check, "save_state"), patch.object(
            check, "safe_send_telegram"
        ) as send:
            with self.assertRaises(check.CGVBlockedError):
                check.api_get(session, "test", {}, self.state)

        self.assertEqual(session.calls, 1)
        self.assertEqual(self.state["global_block"]["count"], 1)
        self.assertGreater(check.global_block_remaining(self.state), 0)
        send.assert_not_called()

    def test_second_block_sends_one_outage_alert_and_uses_longer_backoff(self):
        with patch.object(check, "save_state"), patch.object(
            check, "safe_send_telegram", return_value=True
        ) as send:
            first = check.register_global_block(self.state)
            self.state["global_block"]["until"] = 0
            second = check.register_global_block(self.state)
            third = check.register_global_block(self.state)

        self.assertEqual(first, 300)
        self.assertEqual(second, 900)
        self.assertEqual(third, 3600)
        self.assertEqual(send.call_count, 1)
        self.assertTrue(self.state["global_block"]["alerted"])

    def test_success_clears_block_and_sends_recovery_alert(self):
        self.state["global_block"] = {
            "count": 2,
            "until": 0,
            "alerted": True,
        }
        session = FakeSession([FakeResponse(200)])

        with patch.object(check, "save_state"), patch.object(
            check, "safe_send_telegram", return_value=True
        ) as send:
            result = check.api_get(session, "test", {}, self.state)

        self.assertEqual(result, [])
        self.assertEqual(self.state["global_block"]["count"], 0)
        send.assert_called_once()
        self.assertIn("복구", send.call_args.args[0])

    def test_new_runner_clears_cooldown_but_preserves_outage_alert(self):
        self.state["global_block"] = {
            "count": 8,
            "until": 9999999999,
            "alerted": True,
        }

        result = check.prepare_runtime_state(self.state)

        self.assertEqual(result["global_block"]["count"], 0)
        self.assertEqual(result["global_block"]["until"], 0)
        self.assertTrue(result["global_block"]["alerted"])


if __name__ == "__main__":
    unittest.main()
