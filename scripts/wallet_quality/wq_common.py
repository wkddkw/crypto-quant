"""Shared helpers for the wallet-quality research pipeline (read-only).

Rules enforced here:
- Keys come from env vars only and are never written to any artifact.
- Wallet addresses are pseudonymized with a truncated sha256 in all
  committed/report outputs; raw addresses stay inside gitignored data.
- All HTTP for this pipeline is public, read-only data. No signing, no
  order submission, no live-mode fields anywhere.
- Fail closed: missing key/config aborts rather than silently skipping.
"""
import hashlib
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads((Path(__file__).resolve().parent / "wq_config.json").read_text())
DATA = ROOT / "data" / "wallet_quality"
RAW = DATA / "raw"
SALT_ENV = "WQ_PSEUDO_SALT"


def require_key():
    key = os.environ.get(CONFIG["helius_api_key_env"], "").strip()
    if not key:
        raise RuntimeError(
            f"missing_{CONFIG['helius_api_key_env']}: export the Helius dev key first; "
            "this pipeline fails closed without it"
        )
    return key


def pseudo(address):
    """Stable truncated hash id. Salt must be constant per study run."""
    salt = os.environ.get(SALT_ENV, "")
    if not salt:
        raise RuntimeError("missing_WQ_PSEUDO_SALT")
    return hashlib.sha256((salt + address).encode()).hexdigest()[:10]


def http_proxies(url):
    """Proxy policy shared with the main app: NO_PROXY env or direct URL wins."""
    if os.environ.get("CRYPTO_QUANT_NO_PROXY", "").strip() == "1":
        return None
    if url.startswith(CONFIG["enhanced_url"]) or url.startswith(CONFIG["rpc_url"]):
        # Helius is reachable directly from both this Mac and the Grok node.
        return None
    proxy = CONFIG.get("proxy")
    return {"http": proxy, "https": proxy} if proxy else None


class Throttle:
    def __init__(self, rps):
        self.min_gap = 1.0 / float(rps)
        self._last = 0.0

    def wait(self):
        now = time.time()
        delta = self._last + self.min_gap - now
        if delta > 0:
            time.sleep(delta)
        self._last = time.time()


_THROTTLES = {}


def throttle(bucket="helius"):
    if bucket not in _THROTTLES:
        _THROTTLES[bucket] = Throttle(CONFIG["rate_limit_rps"])
    return _THROTTLES[bucket]


def http_get_json(url, params=None, timeout=25, bucket="other", retries=4):
    import requests

    last = None
    for i in range(retries):
        throttle(bucket).wait()
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             proxies=http_proxies(url),
                             headers={"User-Agent": "crypto-quant-wq/0.1"})
            if r.status_code == 200:
                return r.json()
            if r.status_code in (400, 401, 404):
                raise RuntimeError(f"http_{r.status_code}_for_{url.split('?')[0]}")
            last = f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            last = repr(exc)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def http_post_json(url, payload, timeout=30, bucket="helius", retries=4):
    import requests

    last = None
    for i in range(retries):
        throttle(bucket).wait()
        try:
            r = requests.post(url, json=payload, timeout=timeout,
                              proxies=http_proxies(url),
                              headers={"User-Agent": "crypto-quant-wq/0.1"})
            if r.status_code == 200:
                body = r.json()
                if isinstance(body, dict) and body.get("error"):
                    last = str(body["error"])[:200]
                else:
                    return body
            else:
                last = f"HTTP {r.status_code} {r.text[:150]}"
        except requests.RequestException as exc:
            last = repr(exc)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"POST {url} failed: {last}")


def ensure_dirs():
    RAW.mkdir(parents=True, exist_ok=True)


def append_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_jsonl(path):
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def is_stable_or_wsol(mint):
    """Value media (stable/wrapped majors/SOL), not token signals."""
    return (mint == CONFIG["wsol_mint"] or mint in CONFIG["stables"]
            or mint in CONFIG.get("wrapped_majors", []))
