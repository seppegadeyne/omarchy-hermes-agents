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
# The probe-reuse window is tested on its own below; off for the older cases.
huc.LIMITS_MIN_PROBE_INTERVAL_S = 0

GOOD_LIMITS = [{"label": "Weekly (10/100)", "percent": 0.1, "resetsAt": "2026-09-15T00:00:00+00:00"}]


def check(cond, msg):
    if not cond:
        failures.append(msg)


# --- transient detector -----------------------------------------------------
check(
    huc._is_transient_fetch_error(
        "ChatGPT usage unavailable",
        "<urlopen error [Errno -3] Temporary failure in name resolution>",
    ),
    "DNS failure not flagged transient",
)
check(
    not huc._is_transient_fetch_error(
        "ChatGPT not authenticated", "Run `hermes auth add openai-codex --type oauth`"
    ),
    "invalid-key flagged transient (must not be)",
)

# --- success populates cache ------------------------------------------------
def good_fetcher(auth):
    return (list(GOOD_LIMITS), "Coding Plan · Max", "", "")


limits, tier, status, help_ = huc.fetch_limits_resilient("chatgpt", good_fetcher, {})
check(limits == GOOD_LIMITS and not status, f"good fetch mangled: {limits} {status}")
check(huc.LIMITS_CACHE_PATH.exists(), "cache not written after good fetch")

# --- transient failure retries, then serves cache ---------------------------
calls = {"n": 0}

def flaky_fetcher(auth):
    calls["n"] += 1
    return ([], "", "ChatGPT usage unavailable",
            "<urlopen error [Errno -3] Temporary failure in name resolution>")


huc.time.sleep = lambda s: None  # keep the test instant
limits, tier, status, help_ = huc.fetch_limits_resilient("chatgpt", flaky_fetcher, {})
check(calls["n"] == 2, f"expected 2 attempts (1 retry), got {calls['n']}")
check(limits == GOOD_LIMITS, f"cached limits not served: {limits}")
check(status == "ChatGPT usage unavailable — showing cached limits",
      f"unexpected cached status: {status!r}")
check(tier == "Coding Plan · Max", f"cached tier not served: {tier!r}")

# --- auth errors: no retry, no cache masking --------------------------------
calls["n"] = 0

def auth_fetcher(auth):
    calls["n"] += 1
    return ([], "", "ChatGPT not authenticated", "Run `hermes auth add openai-codex --type oauth`")


limits, tier, status, help_ = huc.fetch_limits_resilient("chatgpt", auth_fetcher, {})
check(calls["n"] == 1, f"auth error retried ({calls['n']} attempts), must not be")
check(not limits, "auth error served cached limits, must not")
check("cached" not in status, f"auth error got cached-status: {status!r}")

# --- stale cache (> 2h) is not served ---------------------------------------
cache = huc._load_limits_cache()
cache["chatgpt"]["storedAt"] = _time.time() - (3 * 60 * 60)
huc._store_cached_limits(cache, "chatgpt", cache["chatgpt"])
limits, tier, status, help_ = huc.fetch_limits_resilient("chatgpt", flaky_fetcher, {})
check(not limits, "stale cache (>2h) served, must not be")
check("cached" not in status, f"stale cache status: {status!r}")

# --- _parse_args: orchestrator call convention --------------------------------
lo, only, excl = huc._parse_args(["--limits-only", "chatgpt"])
check(lo is True and only == {"chatgpt"} and excl == set(), f"parse --limits-only chatgpt: {lo} {only} {excl}")
lo, only, excl = huc._parse_args(["--force", "--except", "claude", "--except", "codex", "claude-cli", "chatgpt"])
check(lo is False and only == {"claude-cli", "chatgpt"} and excl == {"claude", "codex"},
      f"parse force/except/ids: {lo} {only} {excl}")
lo, only, excl = huc._parse_args([])
check(lo is False and only == set() and excl == set(), f"parse empty: {lo} {only} {excl}")

# --- retryAdvised policy ------------------------------------------------------
check(huc._retry_advised("ChatGPT usage unavailable", "name resolution"), "transient should advise retry")
check(not huc._retry_advised("ChatGPT not authenticated", "Run `hermes auth add openai-codex --type oauth`"), "auth error must not advise retry")
check(not huc._retry_advised("", ""), "success must not advise retry")

# --- probe reuse window: a recent probe is served without a request ---------
huc.LIMITS_MIN_PROBE_INTERVAL_S = 180
calls["n"] = 0

def counting_good(auth):
    calls["n"] += 1
    return (list(GOOD_LIMITS), "Max", "", "")


huc.fetch_limits_resilient("claude-cli", counting_good, {})
huc.fetch_limits_resilient("claude-cli", counting_good, {})
check(calls["n"] == 1, f"probe within reuse window hit the endpoint ({calls['n']} calls)")
huc.fetch_limits_resilient("claude-cli", counting_good, {}, force=True)
check(calls["n"] == 2, "--force must skip the reuse window")

# --- 429: cooldown, cached limits, no red banner, no hammering ----------------
huc.LIMITS_MIN_PROBE_INTERVAL_S = 0
calls["n"] = 0

def rate_limited(auth):
    calls["n"] += 1
    return ([], "", huc.RATE_LIMITED_STATUS, "retry after 900s")


limits, tier, status, help_ = huc.fetch_limits_resilient("claude-cli", rate_limited, {})
check(calls["n"] == 1, f"429 retried in-process ({calls['n']} calls)")
check(limits == GOOD_LIMITS and status == "", f"429 should serve cached limits quietly: {limits} {status!r}")
check(huc.LAST_FETCH_META["claude-cli"]["stale"] is True, "429 cached limits must be marked stale")
until = huc._load_limits_cache()["claude-cli"]["rateLimitedUntil"]
check(until - _time.time() > 850, f"Retry-After 900s not honoured: {until - _time.time():.0f}s")
limits, tier, status, help_ = huc.fetch_limits_resilient("claude-cli", rate_limited, {}, force=True)
check(calls["n"] == 1, "probe during cooldown (even --force) must not hit the endpoint")
check(limits == GOOD_LIMITS, "cooldown should keep serving cached limits")
check(not huc._retry_advised(status, help_), "429 must not advise the 30s retry")

# 429 without any cached limits: honest status, still no hammering.
cache = huc._load_limits_cache()
cache.pop("claude-cli")
huc._store_cached_limits(cache, "other", {"limits": []})
calls["n"] = 0
limits, tier, status, help_ = huc.fetch_limits_resilient("claude-cli", rate_limited, {})
check(not limits and status == huc.RATE_LIMITED_STATUS, f"no-cache 429 status: {status!r}")
huc.fetch_limits_resilient("claude-cli", rate_limited, {})
check(calls["n"] == 1, "no-cache 429 must also cool down")

# --- claude CLI self-refresh nudge -------------------------------------------
# The collector never writes the credential file; it only spawns the CLI,
# which refreshes under its own lock. Stub the CLI + credentials + HTTP.
import json as _json
import subprocess as _subprocess
import urllib.error as _uerr

_tmp = Path(tempfile.mkdtemp())
huc.CLAUDE_CREDENTIALS = _tmp / ".credentials.json"
huc.CLAUDE_NUDGE_STATE = _tmp / "claude-cli-nudge.json"
_fake_bin = _tmp / "claude"
_fake_bin.write_text("")
import shutil as _shutil
_shutil_which = _shutil.which
_shutil.which = lambda name: str(_fake_bin) if name == "claude" else _shutil_which(name)


def _write_cred(token, expires_s):
    huc.CLAUDE_CREDENTIALS.write_text(_json.dumps(
        {"claudeAiOauth": {"accessToken": token, "expiresAt": int(expires_s * 1000)}}))


spawns = {"n": 0, "refresh": True}


def fake_run(cmd, **kw):
    spawns["n"] += 1
    check(cmd[0] == str(_fake_bin) and "-p" in cmd, f"unexpected nudge cmd: {cmd}")
    if spawns["refresh"]:
        _write_cred(f"fresh{spawns['n']}", _time.time() + 8 * 3600)
    return _subprocess.CompletedProcess(cmd, 0, "ok", "")


_subprocess.run = fake_run
seen_tokens = []
http_401_for = set()


def fake_usage(token):
    seen_tokens.append(token)
    if token in http_401_for:
        raise _uerr.HTTPError("u", 401, "unauthorized", {}, None)
    return {"five_hour": {"utilization": 13.0, "resets_at": "2026-10-19T12:49:59+00:00"}}


huc._claude_usage_request = fake_usage

# Expired token -> CLI nudged once -> fresh token used, meters come back.
_write_cred("old", _time.time() - 60)
limits, tier, status, help_ = huc.fetch_claude_cli_limits({})
check(spawns["n"] == 1, f"expired token should nudge the CLI once, got {spawns['n']}")
check(seen_tokens[-1] == "fresh1", f"fresh token not used: {seen_tokens}")
check(limits and not status, f"limits after nudge: {limits} {status!r}")

# Valid token: no nudge.
limits, *_ = huc.fetch_claude_cli_limits({})
check(spawns["n"] == 1, "valid token must not spawn the CLI")

# Throttle: expired again within 10 min -> no second spawn, honest status.
_write_cred("old2", _time.time() - 60)
limits, tier, status, help_ = huc.fetch_claude_cli_limits({})
check(spawns["n"] == 1, "nudge must be throttled to once per interval")
check(status == "Claude sign-in expired" and not limits, f"throttled status: {status!r}")

# Failed nudge (dead login) backs off for an hour, not 10 min.
huc.CLAUDE_NUDGE_STATE.unlink()
spawns["refresh"] = False
huc.fetch_claude_cli_limits({})
check(spawns["n"] == 2, "nudge should run after the state reset")
st = _json.loads(huc.CLAUDE_NUDGE_STATE.read_text())
st["lastAttemptAt"] -= 15 * 60
huc.CLAUDE_NUDGE_STATE.write_text(_json.dumps(st))
huc.fetch_claude_cli_limits({})
check(spawns["n"] == 2, "failed nudge must back off longer than 10 min")

# 401 on a not-yet-expired token -> nudge, retry with the fresh token.
huc.CLAUDE_NUDGE_STATE.unlink()
spawns["refresh"] = True
_write_cred("revoked", _time.time() + 3600)
http_401_for.add("revoked")
limits, tier, status, help_ = huc.fetch_claude_cli_limits({})
check(spawns["n"] == 3 and seen_tokens[-1] == "fresh3", f"401 path: {spawns} {seen_tokens[-2:]}")
check(limits and not status, f"401 path should recover: {status!r}")
check(_json.loads(huc.CLAUDE_CREDENTIALS.read_text())["claudeAiOauth"]["accessToken"] == "fresh3",
      "collector must not write the credential file itself")

if failures:
    print("FAIL")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("PASS: all resilience cases OK")
