import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from requests.exceptions import ConnectionError, ConnectTimeout, ProxyError, ReadTimeout

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import http_transport as ht  # noqa: E402


def _response(status=200, text="ok"):
    response = Mock()
    response.status_code = status
    response.text = text
    response.json.return_value = {"ok": True, "data": [{"last": "1"}]}
    return response


class HttpTransportTests(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.pop("CRYPTO_QUANT_NO_PROXY", None)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("CRYPTO_QUANT_NO_PROXY", None)
        else:
            os.environ["CRYPTO_QUANT_NO_PROXY"] = self._old_env

    def test_no_proxy_env_skips_configured_proxy(self):
        os.environ["CRYPTO_QUANT_NO_PROXY"] = "1"
        ok = _response()
        with patch("http_transport.requests.get", return_value=ok) as mock_get:
            result = ht.request_get("https://example.test/x",
                                    proxy="http://127.0.0.1:10808")
        self.assertIs(result, ok)
        mock_get.assert_called_once()
        self.assertIsNone(mock_get.call_args.kwargs["proxies"])

    def test_uses_proxy_when_connect_succeeds(self):
        ok = _response()
        with patch("http_transport.requests.get", return_value=ok) as mock_get:
            result = ht.request_get("https://example.test/x",
                                    proxy="http://127.0.0.1:10808", timeout=20)
        self.assertIs(result, ok)
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs["proxies"],
                         {"http": "http://127.0.0.1:10808", "https": "http://127.0.0.1:10808"})
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 20)

    def test_falls_back_to_direct_on_proxy_connection_error(self):
        ok = _response()

        def side_effect(*_args, **kwargs):
            if kwargs.get("proxies"):
                raise ConnectionError("proxy refused")
            return ok

        with patch("http_transport.requests.get", side_effect=side_effect) as mock_get:
            result = ht.request_get("https://example.test/x",
                                    proxy="http://127.0.0.1:10808")
        self.assertIs(result, ok)
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_get.call_args_list[0].kwargs["proxies"],
                         {"http": "http://127.0.0.1:10808", "https": "http://127.0.0.1:10808"})
        self.assertIsNone(mock_get.call_args_list[1].kwargs["proxies"])

    def test_falls_back_on_proxyerror_and_connect_timeout(self):
        ok = _response()
        for exc in (ProxyError("bad proxy"), ConnectTimeout("proxy connect")):
            with self.subTest(exc=type(exc).__name__):
                def side_effect(*_args, _exc=exc, **kwargs):
                    if kwargs.get("proxies"):
                        raise _exc
                    return ok
                with patch("http_transport.requests.get", side_effect=side_effect) as mock_get:
                    result = ht.request_get("https://example.test/x",
                                            proxy="http://127.0.0.1:10808")
                self.assertIs(result, ok)
                self.assertEqual(mock_get.call_count, 2)

    def test_fail_closed_when_proxy_and_direct_fail(self):
        def side_effect(*_args, **kwargs):
            if kwargs.get("proxies"):
                raise ProxyError("proxy down")
            raise ConnectionError("direct down")

        with patch("http_transport.requests.get", side_effect=side_effect) as mock_get:
            with self.assertRaises(ConnectionError) as ctx:
                ht.request_get("https://example.test/x", proxy="http://127.0.0.1:10808")
        self.assertEqual(mock_get.call_count, 2)
        self.assertIsInstance(ctx.exception.__cause__, ProxyError)

    def test_http_status_does_not_trigger_fallback(self):
        bad = _response(status=502, text="bad gateway")
        with patch("http_transport.requests.get", return_value=bad) as mock_get:
            result = ht.request_get("https://example.test/x",
                                    proxy="http://127.0.0.1:10808")
        self.assertIs(result, bad)
        self.assertEqual(result.status_code, 502)
        mock_get.assert_called_once()

    def test_read_timeout_does_not_trigger_fallback(self):
        with patch("http_transport.requests.get", side_effect=ReadTimeout("slow origin")) as mock_get:
            with self.assertRaises(ReadTimeout):
                ht.request_get("https://example.test/x", proxy="http://127.0.0.1:10808")
        mock_get.assert_called_once()

    def test_use_proxy_false_is_direct_only(self):
        ok = _response()
        with patch("http_transport.requests.get", return_value=ok) as mock_get:
            ht.request_get("https://example.test/x",
                           proxy="http://127.0.0.1:10808", use_proxy=False)
        mock_get.assert_called_once()
        self.assertIsNone(mock_get.call_args.kwargs["proxies"])

    def test_empty_proxy_is_direct_only(self):
        ok = _response()
        with patch("http_transport.requests.get", return_value=ok) as mock_get:
            ht.request_get("https://example.test/x", proxy="", use_proxy=True)
        mock_get.assert_called_once()
        self.assertIsNone(mock_get.call_args.kwargs["proxies"])


class PublicClientWiringTests(unittest.TestCase):
    def _load(self, name):
        spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def test_public_data_clients_call_shared_transport(self):
        collector = self._load("collector")
        carry = self._load("carry_trader")
        paper = self._load("paper_trader")
        pm = self._load("polymarket_data")
        self.assertIs(collector.request_get, ht.request_get)
        self.assertIs(carry.request_get, ht.request_get)
        self.assertIs(paper.request_get, ht.request_get)
        self.assertIs(pm.request_get, ht.request_get)

    def test_collector_http_get_passes_proxy_policy(self):
        collector = self._load("collector")
        ok = _response()
        with patch.object(collector, "request_get", return_value=ok) as mock_get, \
             patch.object(collector.time, "sleep"):
            collector.http_get("https://example.test/x", {"q": 1})
            collector.http_get("https://example.test/cm", use_proxy=False)
        self.assertEqual(mock_get.call_count, 2)
        first = mock_get.call_args_list[0].kwargs
        self.assertEqual(first["proxy"], collector.CFG["proxy"])
        self.assertTrue(first["use_proxy"])
        self.assertFalse(mock_get.call_args_list[1].kwargs["use_proxy"])

    def test_carry_and_polymarket_get_use_transport(self):
        carry = self._load("carry_trader")
        pm = self._load("polymarket_data")
        ok = _response()
        ok.raise_for_status = Mock()
        with patch.object(carry, "request_get", return_value=ok) as carry_get:
            carry.get("https://example.test/okx", {"instId": "BTC-USDT"})
        carry_get.assert_called_once()
        self.assertEqual(carry_get.call_args.kwargs["proxy"], carry.CONFIG["proxy"])
        with patch.object(pm, "request_get", return_value=ok) as pm_get:
            pm.get("https://example.test", "/markets", {"limit": 1})
        pm_get.assert_called_once()
        self.assertEqual(pm_get.call_args.kwargs["proxy"], pm.CONFIG["proxy"])

    def test_gmgn_adapter_not_rewired(self):
        source = (ROOT / "gmgn_adapter.py").read_text()
        self.assertNotIn("http_transport", source)
        self.assertIn('CONFIG["mode"] == "fixture"', source)
        self.assertIn('CONFIG["mode"] == "live"', source)


if __name__ == "__main__":
    unittest.main()
