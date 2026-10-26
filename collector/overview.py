"""Hermes overview + live local-model records for the Omarchy agents panel.

Imported by hermes-usage-collector (same directory when deployed:
~/.local/lib/hermes-usage-collector/overview.py). Two records:

hermes-overview  Hermes as the orchestrator: today's work across EVERY
                 provider and profile, its API-price value, cron health,
                 gateway state and the per-profile split. All of it is
                 shown as rows (`limits`), never as top-level token stats:
                 those tokens are already counted under Claude/ChatGPT/local
                 and the panel header sums top-level stats across tabs.

local-live       Self-hosted work (Hermes rows whose base URL is loopback,
                 private or a LAN name) plus live hardware state: which
                 local servers answer and with which model, VRAM, GPU power,
                 energy today and how much of the fast tier ran locally.
                 Replaces the stock `local` record, which counted the
                 claude CLI's `haiku`/`opus` aliases as local models.

Row conventions (understood by the seppe.agents Panel.qml patches):
  percent  0..1 draws a meter; -1 is an info row (no meter)
  value    short right-aligned text (replaces the reset countdown)
  detail   extra tooltip lines
  urgent   paints the row in the alarm color
  alarm    false keeps a full meter (VRAM, power) from turning the bar red
"""

from __future__ import annotations

import ipaddress
import json
import os
import sqlite3
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

HERMES_HOME = Path.home() / ".hermes"
PROFILES_DIR = HERMES_HOME / "profiles"
AGENTS_STATE = Path.home() / ".local" / "state" / "omarchy" / "agents"
ENERGY_STATE = AGENTS_STATE / "local-live-energy.json"      # sidecar: never inside usage/
LOCAL_CONFIG = Path.home() / ".config" / "omarchy" / "agents" / "local-live.json"

OVERVIEW_ID = "hermes-overview"
LOCAL_ID = "local-live"

# Ports local runtimes listen on by default; probed besides every
# self-hosted base_url found in the Hermes configs.
DEFAULT_LOCAL_URLS = (
    "http://127.0.0.1:11434/v1",   # ollama
    "http://127.0.0.1:1234/v1",    # lm studio
    "http://127.0.0.1:5000/v1",    # tabbyAPI
)
PROBE_TIMEOUT_S = 0.6
DEFAULT_EUR_PER_KWH = 0.30
ENERGY_GAP_MAX_S = 20 * 60        # longer gaps (sleep, collector down) are not integrated


# ── shared helpers ─────────────────────────────────────────────────────────


def profile_homes():
    """(name, home) for the default profile and every dedicated profile."""
    homes = [("default", HERMES_HOME)]
    if PROFILES_DIR.is_dir():
        homes += [(p.name, p) for p in sorted(PROFILES_DIR.iterdir()) if p.is_dir()]
    return homes


def open_ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)


def self_hosted(url):
    """Same rule as the stock collectors' self_hosted(): loopback, private,
    link-local, bare LAN names and home-router suffixes."""
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback or addr.is_private or addr.is_link_local
    except ValueError:
        pass
    if "." not in host:
        return True
    return host.endswith((".home", ".lan", ".local", ".internal", ".localdomain", ".home.arpa"))


def fmt_tokens(n):
    n = float(n or 0)
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= size:
            v = n / size
            return f"{v:.0f}{unit}" if v >= 100 else f"{v:.1f}{unit}".replace(".0" + unit, unit)
    return f"{n:.0f}"


def fmt_usd(v):
    v = float(v or 0)
    return f"${v:,.0f}" if v >= 100 else f"${v:,.2f}"


def info(title, value="", detail="", urgent=False):
    row = {"label": title, "title": title, "percent": -1, "resetsAt": "", "value": value}
    if detail:
        row["detail"] = detail
    if urgent:
        row["urgent"] = True
    return row


def meter(title, percent, value="", detail="", alarm=False):
    row = {"label": title, "title": title, "percent": max(0.0, min(1.0, float(percent))),
           "resetsAt": "", "value": value}
    if detail:
        row["detail"] = detail
    if not alarm:
        row["alarm"] = False
    return row


def local_midnight_epoch():
    now = datetime.now().astimezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def base_record(rid, name):
    return {
        "schemaVersion": 1,
        "id": rid,
        "name": name,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "ready": True,
        "hasLocalStats": True,
        "hasPromptStats": True,
        "scope": "machine",
        "tierLabel": "",
        "usageStatusText": "",
        "authHelpText": "",
        "retryAdvised": False,
        "limits": [],
        "limitsStale": False,
        "limitsFetchedAt": int(time.time() * 1000),
        "todayPrompts": 0,
        "todaySessions": 0,
        "todayTotalTokens": 0,
        "todayTokensByModel": {},
        "recentDays": [],
        "totalPrompts": 0,
        "totalSessions": 0,
        "activeDays": 0,
        "activeDates": [],
        "modelUsage": {},
    }


# ── usage rows from every state.db ─────────────────────────────────────────

USAGE_SQL = """
SELECT smu.session_id, smu.model, smu.billing_provider, smu.billing_base_url,
       coalesce(smu.input_tokens,0), coalesce(smu.output_tokens,0),
       coalesce(smu.cache_read_tokens,0), coalesce(smu.cache_write_tokens,0),
       coalesce(smu.api_call_count,0), coalesce(smu.estimated_cost_usd,0),
       smu.first_seen, smu.last_seen, s.source
FROM session_model_usage smu JOIN sessions s ON s.id = smu.session_id
"""


def usage_rows():
    """Yield dicts per (profile, session, model) usage row, plus unreadable DBs."""
    rows, unreadable = [], []
    for name, home in profile_homes():
        db = home / "state.db"
        if not db.exists():
            continue
        try:
            conn = open_ro(db)
            try:
                for r in conn.execute(USAGE_SQL):
                    rows.append({
                        "profile": name, "session": (name, r[0]), "model": r[1] or "unknown",
                        "provider": r[2] or "", "url": r[3] or "",
                        "inp": r[4], "out": r[5], "cr": r[6], "cw": r[7],
                        "calls": r[8], "cost": r[9], "first": r[10] or 0, "last": r[11] or 0,
                        "source": r[12] or "",
                    })
            finally:
                conn.close()
        except sqlite3.Error as exc:
            unreadable.append(f"{name}: {exc}")
    return rows, unreadable


def row_total(r):
    return r["inp"] + r["out"] + r["cr"] + r["cw"]


# ── hermes-overview ─────────────────────────────────────────────────────────


def cron_health(midnight):
    ok = failed = running = 0
    failing, nxt = [], None
    jobs_enabled = 0
    for name, home in profile_homes():
        cron = home / "cron"
        try:
            jobs = json.loads((cron / "jobs.json").read_text())
            jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else jobs
        except Exception:
            jobs = []
        for job in jobs:
            if not isinstance(job, dict) or not job.get("enabled"):
                continue
            jobs_enabled += 1
            label = str(job.get("name") or job.get("id") or "?")[:40]
            if name != "default":
                label = f"{name}: {label}"
            status = str(job.get("last_status") or "").lower()
            if status and status not in ("ok", "success", "completed"):
                failing.append(f"{label} ({status})")
            when = job.get("next_run_at")
            if when:
                try:
                    t = datetime.fromisoformat(str(when)).timestamp()
                    if t > time.time() and (nxt is None or t < nxt[0]):
                        nxt = (t, label)
                except ValueError:
                    pass
        db = cron / "executions.db"
        if db.exists():
            try:
                conn = open_ro(db)
                try:
                    # julianday() compares instants, whatever offset a
                    # writer stored (+01:00 local or +00:00 UTC).
                    since = datetime.fromtimestamp(midnight, timezone.utc).isoformat()
                    for status, n in conn.execute(
                            "SELECT status, count(*) FROM executions "
                            "WHERE julianday(claimed_at) >= julianday(?) GROUP BY 1", (since,)):
                        if status == "completed":
                            ok += n
                        elif status == "failed":
                            failed += n
                        elif status in ("claimed", "running"):
                            running += n
                finally:
                    conn.close()
            except sqlite3.Error:
                pass
    total = ok + failed
    detail = [f"{jobs_enabled} enabled jobs across profiles"]
    if running:
        detail.append(f"{running} running now")
    if failed:
        detail.append(f"{failed} failed run(s) today")
    detail += [f"Last run failed: {f}" for f in failing[:5]]
    if nxt:
        detail.append(f"Next: {nxt[1]} at {datetime.fromtimestamp(nxt[0]).strftime('%H:%M')}")
    value = f"{ok}/{total}" if total else "—"
    return info("Cron runs today", value, "\n".join(detail), urgent=bool(failed or failing))


def gateway_row():
    try:
        st = json.loads((HERMES_HOME / "gateway_state.json").read_text())
    except Exception:
        return info("Gateway", "Off", "No gateway_state.json", urgent=True), 0
    pid = int(st.get("pid") or 0)
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    running = alive and st.get("gateway_state") == "running"
    plats = st.get("platforms") or {}
    bad = [n for n, p in plats.items()
           if isinstance(p, dict) and (p.get("state") != "connected" or p.get("needs_attention"))]
    good = [n for n in plats if n not in bad]
    active = int(st.get("active_agents") or 0) if running else 0
    detail = []
    if good:
        detail.append("Connected: " + ", ".join(sorted(good)))
    if bad:
        detail.append("Needs attention: " + ", ".join(sorted(bad)))
    detail.append(f"{active} agent(s) active")
    value = ("Online" if not bad else "Degraded") if running else "Offline"
    return info("Gateway", value, "\n".join(detail), urgent=not running or bool(bad)), active


def collect_overview(rows, unreadable):
    rec = base_record(OVERVIEW_ID, "Hermes")
    # Tokens are already counted under the provider tabs; keep them out of
    # the header sums and the TOKENS BY MODEL aggregate.
    rec["hasPromptStats"] = False
    rec["hasLocalStats"] = False

    midnight = local_midnight_epoch()
    week_start = midnight - 6 * 86400
    used = [r for r in rows if row_total(r) > 0 or r["calls"] > 0]
    today = [r for r in used if r["last"] >= midnight]
    week = [r for r in used if r["last"] >= week_start]

    sessions = {r["session"] for r in today}
    prompts = sum(max(r["calls"], 1) for r in today)
    tokens = sum(row_total(r) for r in today)
    cost_today = sum(r["cost"] for r in today)
    cost_week = sum(r["cost"] for r in week)

    by_source = {}
    for r in today:
        by_source.setdefault(r["source"] or "other", set()).add(r["session"])
    by_provider = {}
    for r in today:
        key = r["provider"] or "unknown"
        if self_hosted(r["url"]):
            key = "local"
        elif key.startswith("claude-subscription"):
            key = "claude"
        elif key == "openai-codex":
            key = "chatgpt"
        by_provider[key] = by_provider.get(key, 0) + row_total(r)

    limits = []
    limits.append(info(
        "Sessions today", str(len(sessions)),
        f"{prompts:,} prompts · {fmt_tokens(tokens)} tokens\n"
        + " · ".join(f"{k} {len(v)}" for k, v in sorted(by_source.items(), key=lambda kv: -len(kv[1])))))
    limits.append(info(
        "Tokens today", fmt_tokens(tokens),
        "By route: " + ", ".join(f"{k} {fmt_tokens(v)}" for k, v in sorted(by_provider.items(), key=lambda kv: -kv[1]))
        if by_provider else ""))
    limits.append(info(
        "API value today", fmt_usd(cost_today),
        f"Last 7 days: {fmt_usd(cost_week)}\nWhat these sessions would cost at API prices"))

    gw, active = gateway_row()
    limits.append(gw)
    limits.append(cron_health(midnight))

    # Per-profile split of today's tokens (meters, never alarming).
    per_profile = {}
    for r in today:
        per_profile[r["profile"]] = per_profile.get(r["profile"], 0) + row_total(r)
    if len(per_profile) > 1 or (per_profile and "default" not in per_profile):
        for name, n in sorted(per_profile.items(), key=lambda kv: -kv[1])[:5]:
            limits.append(meter(f"Profile {name}", n / tokens if tokens else 0, fmt_tokens(n),
                                f"{n:,} tokens today in profile {name}"))

    rec["limits"] = limits
    rec["tierLabel"] = f"{active} active" if active else ""
    rec["totalSessions"] = len({r["session"] for r in rows})
    if unreadable:
        rec["usageStatusText"] = "Some state.db files unreadable"
        rec["authHelpText"] = "; ".join(unreadable)
    return rec


# ── local-live ──────────────────────────────────────────────────────────────


def configured_local_urls():
    urls = []
    try:
        import yaml  # python-yaml ships with Omarchy
    except Exception:
        yaml = None
    for _, home in profile_homes():
        cfg = home / "config.yaml"
        if yaml is None or not cfg.exists():
            continue
        try:
            data = yaml.safe_load(cfg.read_text()) or {}
        except Exception:
            continue
        for pid, prov in (data.get("providers") or {}).items():
            if isinstance(prov, dict) and self_hosted(prov.get("base_url")):
                urls.append((str(prov.get("name") or pid), prov["base_url"].rstrip("/")))
    for url in DEFAULT_LOCAL_URLS:
        urls.append(("", url))
    seen, out = set(), []
    for name, url in urls:
        key = urlparse(url).netloc
        if key in seen:
            continue
        seen.add(key)
        out.append((name, url))
    return out


def probe_server(url):
    """Return list of model ids served at an OpenAI-compatible URL, or None."""
    try:
        req = urllib.request.Request(url.rstrip("/") + "/models", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            data = json.loads(resp.read() or b"{}")
    except Exception:
        return None
    if not isinstance(data, dict):
        return []          # something answers, but not an OpenAI-style server
    items = data.get("data") or data.get("models") or []
    if not isinstance(items, list):
        return []
    ids = []
    for it in items:
        if isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
            if mid:
                ids.append(os.path.basename(str(mid)))
    return ids


def llama_tps(url):
    """Generation tokens/s from llama.cpp's /metrics (only with --metrics)."""
    parsed = urlparse(url)
    try:
        with urllib.request.urlopen(f"{parsed.scheme}://{parsed.netloc}/metrics", timeout=PROBE_TIMEOUT_S) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception:
        return None
    for line in text.splitlines():
        if line.startswith("llamacpp:predicted_tokens_seconds"):
            try:
                v = float(line.split()[-1])
                return v if v > 0 else None
            except ValueError:
                return None
    return None


def gpu_stats():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,power.draw,power.limit,temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout.strip()
        name, used, total, pw, plim, temp, util = [x.strip() for x in out.splitlines()[0].split(",")]
        procs = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=process_name,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        apps = []
        for line in procs:
            try:   # "[N/A]" memory or an empty name must not cost the GPU rows
                pname, mem = line.rsplit(",", 1)
                apps.append((os.path.basename(pname.strip().split()[0]), int(float(mem))))
            except (ValueError, IndexError):
                continue
        return {"name": name, "used": float(used), "total": float(total), "power": float(pw),
                "limit": float(plim), "temp": float(temp), "util": float(util),
                "apps": sorted(apps, key=lambda a: -a[1])}
    except Exception:
        return None


def fmt_bytes(n):
    n = float(n)
    for unit, size in (("T", 1 << 40), ("G", 1 << 30), ("M", 1 << 20)):
        if n >= size:
            v = n / size
            return f"{v:.0f}{unit}" if v >= 100 else f"{v:.1f}{unit}"
    return f"{n:.0f}B"


def ram_row():
    """RAM in use = MemTotal - MemAvailable (page cache is reclaimable and
    would otherwise make a 128 GB box look full)."""
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, rest = line.split(":", 1)
            mem[key] = int(rest.split()[0]) * 1024
        total, avail = mem["MemTotal"], mem["MemAvailable"]
    except Exception:
        return None
    used = total - avail
    swap_used = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
    detail = (f"{fmt_bytes(used)} of {fmt_bytes(total)} in use · {fmt_bytes(avail)} available\n"
              f"Page cache {fmt_bytes(mem.get('Cached', 0))} (reclaimable, not counted)")
    if swap_used > 0:
        detail += f"\nSwap/zram in use: {fmt_bytes(swap_used)}"
    return meter("RAM", used / total if total else 0, f"{fmt_bytes(used)}", detail)


def disk_row(path=None):
    """Free space on the SSD that holds the models (~/Models, else $HOME)."""
    path = path or (Path.home() / "Models")
    if not Path(path).exists():
        path = Path.home()
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if not total:
        return None
    used = total - st.f_bfree * st.f_frsize
    row = meter("SSD", used / total, f"{fmt_bytes(free)} free",
                f"{fmt_bytes(free)} free of {fmt_bytes(total)} ({used / total:.0%} used)\n"
                f"Filesystem holding {path}")
    if free / total < 0.10:
        row["urgent"] = True
    return row


def energy_today(power_w):
    """Integrate GPU power across collector runs (trapezoid). Sidecar state
    lives outside usage/ (the panel renders every usage/*.json as a tab)."""
    now = time.time()
    day = datetime.now().strftime("%Y-%m-%d")
    try:
        st = json.loads(ENERGY_STATE.read_text())
    except Exception:
        st = {}
    if st.get("day") != day:
        st = {"day": day, "wh": 0.0, "since": now}
    last_t, last_w = st.get("lastAt"), st.get("lastW")
    if power_w is not None and last_t and last_w is not None:
        gap = now - float(last_t)
        if 0 < gap <= ENERGY_GAP_MAX_S:
            st["wh"] = float(st.get("wh", 0)) + (float(last_w) + power_w) / 2 * gap / 3600
    if power_w is not None:
        st["lastAt"], st["lastW"] = now, power_w
    try:
        ENERGY_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ENERGY_STATE.with_name(f".local-live-energy.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(ENERGY_STATE)
    except Exception:
        pass
    return float(st.get("wh", 0)) / 1000.0, float(st.get("since", now))


def eur_per_kwh():
    try:
        return float(json.loads(LOCAL_CONFIG.read_text()).get("eurPerKwh", DEFAULT_EUR_PER_KWH))
    except Exception:
        return DEFAULT_EUR_PER_KWH


def fast_tier_rows(rows):
    """How much of the delegated fast tier (crew explorer/researcher,
    delegate_task fast) ran on a local model vs fell back to haiku, 7 days."""
    since = local_midnight_epoch() - 6 * 86400
    local, cloud = set(), set()
    for r in rows:
        if r["source"] != "subagent" or r["last"] < since:
            continue
        if self_hosted(r["url"]):
            local.add(r["session"])
        elif r["model"] == "haiku":
            cloud.add(r["session"])
    total = len(local) + len(cloud)
    if not total:
        return info("Fast tier local (7d)", "—", "No fast-tier subagent runs in the last 7 days")
    return meter("Fast tier local (7d)", len(local) / total, f"{len(local)}/{total}",
                 f"{len(local)} subagent run(s) on a local model, {len(cloud)} on haiku")


def collect_local(rows, unreadable):
    rec = base_record(LOCAL_ID, "Local models")
    now_dt = datetime.now().astimezone()
    today_str = now_dt.strftime("%Y-%m-%d")
    recent_dates = [(now_dt - timedelta(days=o)).strftime("%Y-%m-%d") for o in range(6, -1, -1)]
    recent = {d: 0 for d in recent_dates}

    local_rows = [r for r in rows if self_hosted(r["url"])]
    model_usage, today_by_model = {}, {}
    sessions, today_sessions, active = set(), set(), set()
    prompts = today_prompts = today_tokens = 0
    for r in local_rows:
        total = row_total(r)
        if total <= 0 and r["calls"] <= 0:
            continue
        b = model_usage.setdefault(r["model"], {"inputTokens": 0, "outputTokens": 0,
                                                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0})
        b["inputTokens"] += r["inp"]
        b["outputTokens"] += r["out"]
        b["cacheReadInputTokens"] += r["cr"]
        b["cacheCreationInputTokens"] += r["cw"]
        p = max(r["calls"], 1)
        prompts += p
        sessions.add(r["session"])
        first = datetime.fromtimestamp(r["first"]).strftime("%Y-%m-%d") if r["first"] else ""
        last = datetime.fromtimestamp(r["last"]).strftime("%Y-%m-%d") if r["last"] else ""
        for d in (first, last):
            if d:
                active.add(d)
        if first and last and first <= today_str <= last:
            today_prompts += p
            today_sessions.add(r["session"])
            today_tokens += total
            today_by_model[r["model"]] = today_by_model.get(r["model"], 0) + total
        for d in recent_dates:
            if first and last and first <= d <= last:
                recent[d] += total

    rec.update({
        "todayPrompts": today_prompts, "todaySessions": len(today_sessions),
        "todayTotalTokens": today_tokens, "todayTokensByModel": today_by_model,
        "recentDays": [{"date": d, "messageCount": recent[d]} for d in recent_dates],
        "totalPrompts": prompts, "totalSessions": len(sessions),
        "activeDays": len(active), "activeDates": sorted(active), "modelUsage": model_usage,
    })

    limits = []
    # Servers: one row per endpoint that answers.
    up = []
    for name, url in configured_local_urls():
        models = probe_server(url)
        if models is None:
            continue
        port = urlparse(url).port or ""
        model = models[0] if models else "no model"
        tps = llama_tps(url)
        detail = [f"{name or 'local server'} at {url}"]
        if len(models) > 1:
            detail.append("Models: " + ", ".join(models[:6]))
        if tps:
            detail.append(f"Generation: {tps:.0f} tok/s (llama.cpp metrics)")
        up.append(info(f"{model} :{port}", f"{tps:.0f} t/s" if tps else "Online", "\n".join(detail)))
    if up:
        limits += up[:4]
    else:
        limits.append(info("Local server", "Offline", "No local OpenAI-compatible endpoint answers"))

    gpu = gpu_stats()
    if gpu:
        apps = ", ".join(f"{n} {m / 1024:.1f}G" for n, m in gpu["apps"][:4])
        limits.append(meter("VRAM", gpu["used"] / gpu["total"] if gpu["total"] else 0,
                            f"{gpu['used'] / 1024:.1f}G",
                            f"{gpu['used'] / 1024:.1f} of {gpu['total'] / 1024:.0f} GB on {gpu['name']}"
                            + (f"\n{apps}" if apps else "")))
        limits.append(meter("GPU power", gpu["power"] / gpu["limit"] if gpu["limit"] else 0,
                            f"{gpu['power']:.0f} W",
                            f"{gpu['temp']:.0f}°C · {gpu['util']:.0f}% utilization · limit {gpu['limit']:.0f} W"))
        kwh, since = energy_today(gpu["power"])
        price = eur_per_kwh()
        limits.append(info("GPU energy today", f"{kwh:.2f} kWh",
                           f"≈ €{kwh * price:.2f} at €{price:.2f}/kWh (set eurPerKwh in {LOCAL_CONFIG})\n"
                           f"Whole GPU, sampled every collector run since "
                           f"{datetime.fromtimestamp(since).strftime('%H:%M')}"))
    for row in (ram_row(), disk_row()):
        if row:
            limits.append(row)
    limits.append(fast_tier_rows(rows))

    rec["limits"] = limits
    rec["tierLabel"] = f"{len(up)} online" if up else "Offline"
    if unreadable:
        rec["usageStatusText"] = "Some state.db files unreadable"
        rec["authHelpText"] = "; ".join(unreadable)
    return rec


def write_record(usage_dir, rec):
    out = usage_dir / f"{rec['id']}.json"
    # Per-process temp name: the timer and a panel-triggered run can overlap.
    tmp = usage_dir / f".{rec['id']}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(rec, indent=2) + "\n")
    tmp.replace(out)
    print(f"Wrote {out.name} ({len(rec['limits'])} rows, {rec['todayTotalTokens']} tokens today)")


def run(usage_dir, only_ids=(), excluded=()):
    wanted = [i for i in (OVERVIEW_ID, LOCAL_ID)
              if (not only_ids or i in only_ids) and i not in excluded]
    if not wanted:
        return
    rows, unreadable = usage_rows()
    if OVERVIEW_ID in wanted:
        write_record(usage_dir, collect_overview(rows, unreadable))
    if LOCAL_ID in wanted:
        write_record(usage_dir, collect_local(rows, unreadable))
