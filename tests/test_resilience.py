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

if failures:
    print("FAIL")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("PASS: all resilience cases OK")
