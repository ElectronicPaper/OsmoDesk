"""Provider boundary tests are synthetic and never use a real credential."""
import io
import http.client
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from driver.ai_provider import (OpenAIProvider, ProviderError, _NoRedirect,
                                parse_envelope, request_payload, strict_json)


def envelope(text='{"treatments": []}', model="gpt-5.6-luna"):
    return {"status": "completed", "model": model,
            "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            "output": [{"type": "reasoning"}, {"type": "message", "role": "assistant",
                       "content": [{"type": "output_text", "text": text}]}]}


class AIProviderTests(unittest.TestCase):
    def test_fixed_bounded_data_only_request(self):
        provider = OpenAIProvider("test-key-never-real")
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(envelope()).encode())
        with patch("driver.ai_provider.urllib.request.build_opener", return_value=opener):
            got = provider.generate({"brief": "reveal"}, {"type": "object"}, model="gpt-5.6-luna", effort="low")
        self.assertEqual(got["model"], "gpt-5.6-luna")
        self.assertEqual(got["usage"]["total_tokens"], 30)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        payload = json.loads(request.data)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["service_tier"], "default")
        self.assertNotIn("tools", payload)
        self.assertNotIn("test-key-never-real", request.data.decode())
        self.assertNotIn("test-key-never-real", repr(provider))
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(payload["text"]["format"]["strict"])

    def test_payload_validation_before_network(self):
        for model, effort, limit in [("other", "low", 3500), ("gpt-6-astra", "none", 3500),
                                     ("gpt-5.6-luna", "low", True), ("gpt-5.6-luna", "low", 999999)]:
            with self.assertRaises(ProviderError):
                request_payload({}, {}, model, effort, limit)
        for context in ({"brief": "x" * 48000}, {"number": float("nan")}):
            with self.assertRaises(ProviderError):
                request_payload(context, {}, "gpt-5.6-luna", "low")

    def test_disabled_without_key_and_invalid_key_timeout(self):
        self.assertFalse(OpenAIProvider("").available)
        with self.assertRaises(ProviderError):
            OpenAIProvider("").generate({}, {}, model="gpt-5.6-luna", effort="low")
        for value in (True, float("nan"), 0, 61):
            with self.assertRaises(ProviderError):
                OpenAIProvider("test", value)
        with self.assertRaises(ProviderError):
            OpenAIProvider("test\r\nheader")

    def test_strict_json_no_duplicate_keys_or_nonfinite(self):
        for text in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{broken'):
            with self.assertRaises(ProviderError):
                strict_json(text)

    def test_output_refusal_incomplete_tools_and_model_substitution(self):
        bad = []
        item = envelope(); item["status"] = "incomplete"; bad.append(item)
        item = envelope(); item["output"].append({"type": "web_search_call"}); bad.append(item)
        item = envelope(); item["output"][-1]["content"] = [{"type": "refusal"}]; bad.append(item)
        item = envelope(); item["output"].append(item["output"][-1]); bad.append(item)
        bad.extend([envelope(model="gpt-5.6-sol"), envelope(model="gpt-5.6-luna-evil")])
        for item in bad:
            with self.assertRaises(ProviderError):
                parse_envelope(item, "gpt-5.6-luna")
        self.assertEqual(parse_envelope(envelope(model="gpt-5.6-luna-2026-09-14"), "gpt-5.6-luna")["model"], "gpt-5.6-luna-2026-09-14")

    def test_usage_unknown_not_zero(self):
        item = envelope(); item["usage"]["input_tokens"] = True
        self.assertIsNone(parse_envelope(item, "gpt-5.6-luna")["usage"])

    def test_errors_sanitized_no_retry(self):
        for error in (urllib.error.HTTPError("url-secret", 429, "secret-body", {}, io.BytesIO(b"secret")),
                      urllib.error.URLError("secret-message"), TimeoutError("secret-message"),
                      http.client.BadStatusLine("secret-message")):
            opener = Mock(); opener.open.side_effect = error
            with patch("driver.ai_provider.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(ProviderError) as raised:
                    OpenAIProvider("test-key").generate({}, {}, model="gpt-5.6-luna", effort="low")
                self.assertNotIn("secret", str(raised.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_deadline_includes_delayed_eof_and_remaining_socket_budget(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read1.return_value = b""
        opener = Mock(); opener.open.return_value = response
        with patch("driver.ai_provider.urllib.request.build_opener", return_value=opener), \
             patch("driver.ai_provider.time.monotonic", side_effect=[0, 44, 46]):
            with self.assertRaisesRegex(ProviderError, "timed out"):
                OpenAIProvider("test", 45).generate({}, {}, model="gpt-5.6-luna", effort="low")
        response.fp.raw._sock.settimeout.assert_called_once_with(1)

    def test_redirects_and_oversized_responses_refused(self):
        with self.assertRaises(ProviderError):
            _NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")
        opener = Mock(); opener.open.return_value = io.BytesIO(b"x" * 1000001)
        with patch("driver.ai_provider.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(ProviderError, "size limit"):
                OpenAIProvider("test").generate({}, {}, model="gpt-5.6-luna", effort="low")
