"""Offline unit tests for the transient-resilience layer in the collector.

Run: python3 tests/test_resilience.py
No network needed: the limits fetchers are stubbed; only the wrapper
logic (retry, caching, auth-error exclusion, staleness cutoff) runs.
"""
import importlib.util
from importlib.machinery import SourceFileLoader
import sys
import tempfile
import time as _time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
loader = SourceFileLoader("huc", str(REPO / "collector" / "hermes-usage-collector"))
spec = importlib.util.spec_from_loader("huc", loader)
assert spec is not None
huc = importlib.util.module_from_spec(spec)
loader.exec_module(huc)

failures = []

# Redirect the cache to a throwaway location for the test run.
setattr(huc, "LIMITS_CACHE_PATH", Path(tempfile.mkdtemp()) / "limits-cache.json")

GOOD_LIMITS = [{"label": "Weekly (10/100)", "percent": 0.1, "resetsAt": "2026-09-15T00:00:00+00:00"}]


def check(cond, msg):
    if not cond:
        failures.append(msg)


# --- transient detector -----------------------------------------------------
check(
    huc._is_transient_fetch_error(
        "Z.AI API unavailable",
        "<urlopen error [Errno -3] Temporary failure in name resolution>",
    ),
    "DNS failure not flagged transient",
)
check(
    not huc._is_transient_fetch_error(
        "Z.AI key invalid", "Check GLM_API_KEY in ~/.hermes/.env"
    ),
    "invalid-key flagged transient (must not be)",
)

# --- success populates cache ------------------------------------------------
def good_fetcher(auth):
    return (list(GOOD_LIMITS), "Coding Plan · Max", "", "")


limits, tier, status, help_ = huc.fetch_limits_resilient("zai", good_fetcher, {})
check(limits == GOOD_LIMITS and not status, f"good fetch mangled: {limits} {status}")
check(huc.LIMITS_CACHE_PATH.exists(), "cache not written after good fetch")

# --- transient failure retries, then serves cache ---------------------------
calls = {"n": 0}

def flaky_fetcher(auth):
    calls["n"] += 1
    return ([], "", "Z.AI API unavailable",
            "<urlopen error [Errno -3] Temporary failure in name resolution>")


huc.time.sleep = lambda s: None  # keep the test instant
limits, tier, status, help_ = huc.fetch_limits_resilient("zai", flaky_fetcher, {})
check(calls["n"] == 2, f"expected 2 attempts (1 retry), got {calls['n']}")
check(limits == GOOD_LIMITS, f"cached limits not served: {limits}")
check(status == "Z.AI API unavailable — showing cached limits",
      f"unexpected cached status: {status!r}")
check(tier == "Coding Plan · Max", f"cached tier not served: {tier!r}")

# --- auth errors: no retry, no cache masking --------------------------------
calls["n"] = 0

def auth_fetcher(auth):
    calls["n"] += 1
    return ([], "", "Z.AI key invalid", "Check GLM_API_KEY in ~/.hermes/.env")


limits, tier, status, help_ = huc.fetch_limits_resilient("zai", auth_fetcher, {})
check(calls["n"] == 1, f"auth error retried ({calls['n']} attempts), must not be")
check(not limits, "auth error served cached limits, must not")
check("cached" not in status, f"auth error got cached-status: {status!r}")

# --- stale cache (> 2h) is not served ---------------------------------------
cache = huc._load_limits_cache()
cache["zai"]["storedAt"] = _time.time() - (3 * 60 * 60)
huc._store_cached_limits(cache, "zai", cache["zai"])
limits, tier, status, help_ = huc.fetch_limits_resilient("zai", flaky_fetcher, {})
check(not limits, "stale cache (>2h) served, must not be")
check("cached" not in status, f"stale cache status: {status!r}")

# --- _parse_args: orchestrator call convention --------------------------------
lo, only, excl = huc._parse_args(["--limits-only", "zai"])
check(lo is True and only == {"zai"} and excl == set(), f"parse --limits-only zai: {lo} {only} {excl}")
lo, only, excl = huc._parse_args(["--force", "--except", "claude", "--except", "kimi", "zai", "chatgpt"])
check(lo is False and only == {"zai", "chatgpt"} and excl == {"claude", "kimi"},
      f"parse force/except/ids: {lo} {only} {excl}")
lo, only, excl = huc._parse_args([])
check(lo is False and only == set() and excl == set(), f"parse empty: {lo} {only} {excl}")

# --- retryAdvised policy ------------------------------------------------------
check(huc._retry_advised("Z.AI API unavailable", "name resolution"), "transient should advise retry")
check(not huc._retry_advised("Z.AI key invalid", "Check GLM_API_KEY"), "auth error must not advise retry")
check(not huc._retry_advised("", ""), "success must not advise retry")

# --- Z.AI multi-key aggregation (pooled keys, mixed plans) ---------------------
# fetch_zai_limits must fetch per key and SUM per window: keys on different
# plans (max + pro) have different allowances, so percent is
# sum(currentValue)/sum(usage) — never a ×2 of one key's numbers.
import json
import urllib.error as _urlerr


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _quota_payload(level, session, weekly):
    # Matches what the real _fetch_zai_quota returns: the UNWRAPPED `data` dict.
    return {
        "level": level,
        "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
             "usage": session[1], "currentValue": session[0],
             "percentage": round(100 * session[0] / session[1]),
             "nextResetTime": session[2]},
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1,
             "usage": weekly[1], "currentValue": weekly[0],
             "percentage": round(100 * weekly[0] / weekly[1]),
             "nextResetTime": weekly[2]},
        ],
    }


saved_keys_fn, saved_fetch = huc.get_zai_api_keys, huc._fetch_zai_quota
try:
    # Two keys: max plan (28000/140000) + pro plan (12000/60000), different resets.
    huc.get_zai_api_keys = lambda auth: ["key-max", "key-pro"]
    huc._fetch_zai_quota = lambda key: (
        _quota_payload("max", (16658, 28000, 1000000000000), (135411, 140000, 2000000000000))
        if key == "key-max"
        else _quota_payload("pro", (0, 12000, 1500000000000), (0, 60000, 3000000000000))
    )
    limits, tier, status, help_ = huc.fetch_zai_limits({})
    check(len(limits) == 2, f"expected 2 windows, got {len(limits)}")
    check(limits[0]["label"] == "Session (5h) (16658/40000)",
          f"session label not summed: {limits[0]['label']!r}")
    check(abs(limits[0]["percent"] - 16658 / 40000) < 1e-9,
          f"session percent not sum/sum: {limits[0]['percent']}")
    check(limits[1]["label"] == "Weekly (135411/200000)",
          f"weekly label not summed: {limits[1]['label']!r}")
    check(limits[0]["resetsAt"].startswith("2001-09-09"),  # 1e12 ms = earliest reset
          f"session reset not earliest: {limits[0]['resetsAt']!r}")
    check(tier == "Coding Plan · Max + Pro", f"mixed-plan tier: {tier!r}")
    check(status == "" and help_ == "", f"healthy pool got status: {status!r} {help_!r}")

    # One healthy key + one 401 key: meters stay, status mentions the failure.
    def _fail_401(key):
        if key == "key-bad":
            raise _urlerr.HTTPError("url", 401, "Unauthorized", {}, None)
        return _quota_payload("max", (100, 28000, 1000000000000), (100, 140000, 2000000000000))

    huc._fetch_zai_quota = _fail_401
    huc.get_zai_api_keys = lambda auth: ["key-good", "key-bad"]
    limits, tier, status, help_ = huc.fetch_zai_limits({})
    check(len(limits) == 2, f"healthy+401 pool lost meters: {len(limits)}")
    check("1/2 keys failed" in status and "invalid" in status,
          f"partial-failure status wrong: {status!r}")
    check(not huc._retry_advised(status, help_), f"401 partial must not advise retry: {status!r}")

    # All keys 401: honest auth failure, no meters, no retry.
    def _fail_all_401(key):
        raise _urlerr.HTTPError("url", 401, "Unauthorized", {}, None)

    huc._fetch_zai_quota = _fail_all_401
    huc.get_zai_api_keys = lambda auth: ["key-bad1", "key-bad2"]
    limits, tier, status, help_ = huc.fetch_zai_limits({})
    check(not limits, "all-401 pool produced meters, must not")
    check(status == "All Z.AI keys invalid", f"all-401 status: {status!r}")
    check(not huc._retry_advised(status, help_), "all-401 must not advise retry")

    # One healthy + one transient failure: meters stay, transient retry advised.
    def _fail_dns(key):
        if key == "key-dns":
            raise OSError("temporary failure in name resolution")
        return _quota_payload("max", (100, 28000, 1000000000000), (100, 140000, 2000000000000))

    huc._fetch_zai_quota = _fail_dns
    huc.get_zai_api_keys = lambda auth: ["key-good", "key-dns"]
    limits, tier, status, help_ = huc.fetch_zai_limits({})
    check(len(limits) == 2, f"healthy+transient pool lost meters: {len(limits)}")
    check(huc._retry_advised(status, help_), f"transient partial must advise retry: {status!r}")

    # Duplicate keys across env vars are de-duplicated (no double counting).
    # Exercise the REAL get_zai_api_keys: same key in two env vars + one more.
    huc._fetch_zai_quota = lambda key: _quota_payload(
        "max", (10, 28000, 1000000000000), (10, 140000, 2000000000000))
    saved_parse_env = huc._parse_env_file
    try:
        huc.get_zai_api_keys = saved_keys_fn  # restore the real resolver
        huc._parse_env_file = lambda path: {
            "GLM_API_KEY": "dup", "ZAI_API_KEY": "dup", "Z_AI_API_KEY": "other"}
        keys = huc.get_zai_api_keys({})
        check(keys == ["dup", "other"], f"env dedupe wrong: {keys!r}")
    finally:
        huc._parse_env_file = saved_parse_env

    calls = {"n": 0}

    def _counting(key):
        calls["n"] += 1
        return _quota_payload("max", (10, 28000, 1000000000000), (10, 140000, 2000000000000))

    huc._fetch_zai_quota = _counting
    huc.get_zai_api_keys = lambda auth: ["dup", "other"]
    limits, tier, status, help_ = huc.fetch_zai_limits({})
    check(calls["n"] == 2, f"two keys fetched {calls['n']} times")
    check(limits[0]["label"] == "Session (5h) (20/56000)", f"two-key sum wrong: {limits[0]['label']!r}")
finally:
    huc.get_zai_api_keys, huc._fetch_zai_quota = saved_keys_fn, saved_fetch


if failures:
    print("FAIL")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("PASS: all resilience cases OK")
