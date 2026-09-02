"""Public-data HTTP GET with optional proxy and one direct fallback.

Used by collector, carry_trader, paper_trader, and polymarket_data.
Does not change committed proxy strings in *_config.json.

Policy:
  - CRYPTO_QUANT_NO_PROXY=1: never use a proxy.
  - use_proxy=False (e.g. Coin Metrics): never use a proxy.
  - empty/missing proxy: direct only.
  - otherwise try the configured proxy; if the connect/proxy path fails,
    retry once without a proxy; fail closed if both fail.

HTTP status codes and read timeouts are not connect/proxy failures and do
not trigger the direct retry.
"""
import os

import requests
from requests.exceptions import (
    ConnectionError,
    ConnectTimeout,
    InvalidProxyURL,
    ProxyError,
    RequestException,
)

NO_PROXY_ENV = "CRYPTO_QUANT_NO_PROXY"


def skip_proxy(use_proxy=True):
    if not use_proxy:
        return True
    return os.environ.get(NO_PROXY_ENV, "").strip() == "1"


def proxy_mapping(proxy_url):
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def is_connect_or_proxy_error(exc):
    return isinstance(exc, (ProxyError, ConnectTimeout, ConnectionError, InvalidProxyURL))


def request_get(url, params=None, headers=None, timeout=25, proxy=None, use_proxy=True):
    """One GET applying the proxy policy. Returns a Response.

    Raises RequestException if the allowed path(s) fail at the transport layer.
    Does not raise for non-2xx HTTP status (callers decide).
    """
    kwargs = {"params": params, "headers": headers, "timeout": timeout}
    if skip_proxy(use_proxy) or not proxy:
        return requests.get(url, proxies=None, **kwargs)

    try:
        return requests.get(url, proxies=proxy_mapping(proxy), **kwargs)
    except RequestException as proxy_exc:
        if not is_connect_or_proxy_error(proxy_exc):
            raise
        try:
            return requests.get(url, proxies=None, **kwargs)
        except RequestException as direct_exc:
            raise direct_exc from proxy_exc
