"""Offline checks for collector/overview.py (run: python3 tests/test_overview.py)."""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
import overview as o  # noqa: E402


def test_self_hosted():
    assert o.self_hosted("http://127.0.0.1:8081/v1")
    assert o.self_hosted("http://localhost:8888/v1")
    assert o.self_hosted("http://192.168.1.5:11434/v1")
    assert o.self_hosted("http://gpubox.lan:8000/v1")
    # The claude CLI route is a process:// pseudo-URL: never local.
    assert not o.self_hosted("process://claude-subscription-directsdk-experimental")
    assert not o.self_hosted("https://chatgpt.com/backend-api/codex")
    assert not o.self_hosted("")


def test_local_excludes_claude_aliases():
    now = time.time()
    rows = [
        {"profile": "default", "session": ("default", "a"), "model": "haiku", "provider": "claude-subscription-directsdk-experimental",
         "url": "process://claude-subscription-directsdk-experimental", "inp": 10, "out": 10, "cr": 0, "cw": 0,
         "calls": 1, "cost": 0.1, "first": now, "last": now, "source": "subagent"},
        {"profile": "default", "session": ("default", "b"), "model": "strata", "provider": "strata-qwen",
         "url": "http://127.0.0.1:8081/v1", "inp": 5, "out": 5, "cr": 0, "cw": 0,
         "calls": 1, "cost": 0.0, "first": now, "last": now, "source": "subagent"},
    ]
    o.configured_local_urls = lambda: []
    o.gpu_stats = lambda: None
    rec = o.collect_local(rows, [])
    assert set(rec["modelUsage"]) == {"strata"}, rec["modelUsage"]
    fast = [r for r in rec["limits"] if r["title"].startswith("Fast tier")][0]
    assert fast["value"] == "1/2" and abs(fast["percent"] - 0.5) < 1e-9


def test_overview_keeps_tokens_out_of_header():
    now = time.time()
    rows = [{"profile": "voltti", "session": ("voltti", "x"), "model": "gpt", "provider": "openai-codex",
             "url": "https://chatgpt.com", "inp": 100, "out": 0, "cr": 0, "cw": 0, "calls": 2,
             "cost": 1.5, "first": now, "last": now, "source": "cron"}]
    rec = o.collect_overview(rows, [])
    assert rec["hasPromptStats"] is False and rec["todayTotalTokens"] == 0
    titles = [r["title"] for r in rec["limits"]]
    assert "API value today" in titles and "Profile voltti" in titles
    for r in rec["limits"]:
        assert r["percent"] == -1 or r.get("alarm") is False, r


def test_energy_integration():
    with tempfile.TemporaryDirectory() as d:
        o.ENERGY_STATE = Path(d) / "energy.json"
        o.energy_today(100.0)
        st = json.loads(o.ENERGY_STATE.read_text())
        st["lastAt"] -= 3600 / 6        # pretend 10 minutes passed
        o.ENERGY_STATE.write_text(json.dumps(st))
        kwh, _ = o.energy_today(100.0)
        assert abs(kwh - 100 / 6 / 1000) < 1e-6, kwh
        # A gap longer than ENERGY_GAP_MAX_S (sleep) is not integrated.
        st = json.loads(o.ENERGY_STATE.read_text())
        st["lastAt"] -= 3 * 3600
        o.ENERGY_STATE.write_text(json.dumps(st))
        kwh2, _ = o.energy_today(100.0)
        assert abs(kwh2 - kwh) < 1e-9


def test_probe_rejects_non_dict_json():
    import io, urllib.request
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: R(b"[1,2]")
    try:
        assert o.probe_server("http://127.0.0.1:5000/v1") == []
    finally:
        urllib.request.urlopen = orig


def test_cron_window_is_offset_agnostic():
    import sqlite3
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        (home / "cron").mkdir()
        db = sqlite3.connect(home / "cron" / "executions.db")
        db.execute("CREATE TABLE executions (status TEXT, claimed_at TEXT)")
        mid = o.local_midnight_epoch()
        from datetime import datetime, timezone
        utc = lambda e: datetime.fromtimestamp(e, timezone.utc).isoformat()
        loc = lambda e: datetime.fromtimestamp(e).astimezone().isoformat()
        db.executemany("INSERT INTO executions VALUES (?,?)", [
            ("completed", utc(mid + 1800)),     # 00:30 local stored in UTC
            ("completed", loc(mid + 3600)),     # 01:00 local stored local
            ("failed", utc(mid - 60)),          # yesterday
        ])
        db.commit(); db.close()
        o.profile_homes = lambda: [("default", home)]
        row = o.cron_health(mid)
        assert row["value"] == "2/2", row



def test_ram_and_disk_rows():
    ram = o.ram_row()
    assert ram and 0 <= ram["percent"] <= 1 and ram["alarm"] is False, ram
    disk = o.disk_row(Path("/"))
    assert disk and disk["value"].endswith(" free") and disk["alarm"] is False, disk


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
