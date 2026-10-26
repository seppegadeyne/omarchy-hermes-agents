#!/usr/bin/env python3
"""Notice when Omarchy ships a new stock agents panel, and dry-run our port.

Runs from the Omarchy post-update and post-boot hooks (as the user). It never
touches the live clone: it fingerprints the stock plugin, and when that
changed since the last check it ports our patch set into a scratch dir and
sends ONE desktop notification per stock version:

  * the port applies and differs from the live clone -> "update available",
    with the command that takes it over (scripts/apply_stock_port.sh);
  * the port applies and equals the live clone -> nothing to do (silent);
  * a patch no longer matches -> which one, so it can be fixed first.

State lives in ~/.local/state/omarchy/agents/stock-port-state.json (outside
usage/: the panel renders every usage/*.json as a tab).

Usage: check_stock_update.py [--stock DIR] [--clone DIR] [--state FILE]
                             [--no-notify] [--baseline]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STOCK = Path("/usr/share/omarchy/shell/plugins/agents")
CLONE = Path.home() / ".config" / "omarchy" / "plugins" / "seppe.agents"
STATE = Path.home() / ".local" / "state" / "omarchy" / "agents" / "stock-port-state.json"
COMPARED = ("Panel.qml", "Main.qml", "Agent.qml")


def fingerprint(stock: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in stock.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(str(path.relative_to(stock)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def package_version() -> str:
    try:
        out = subprocess.run(["pacman", "-Qo", str(STOCK / "Panel.qml")],
                             capture_output=True, text=True, timeout=10).stdout
        return out.strip().split()[-1] if out.strip() else ""
    except Exception:
        return ""


def notify(summary: str, body: str, urgency: str = "normal", enabled: bool = True) -> None:
    print(f"{summary}: {body}")
    if not enabled or not shutil.which("notify-send"):
        return
    subprocess.run(["notify-send", "-a", "Omarchy agents", "-u", urgency, "-i", "system-software-update",
                    summary, body], timeout=10, check=False)


def dry_port(stock: Path) -> tuple[bool, str, Path | None]:
    tmp = Path(tempfile.mkdtemp(prefix="agents-port-check-"))
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "port_from_stock.py"), str(tmp / "out"),
                           "--stock", str(stock)], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        return False, lines[-1] if lines else f"rc={proc.returncode}", None
    return True, "", tmp / "out"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stock", type=Path, default=STOCK)
    ap.add_argument("--clone", type=Path, default=CLONE)
    ap.add_argument("--state", type=Path, default=STATE)
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--baseline", action="store_true", help="record the current stock as handled, no notification")
    args = ap.parse_args()

    if not args.stock.is_dir():
        print(f"no stock plugin at {args.stock}")
        return 0
    fp = fingerprint(args.stock)
    try:
        state = json.loads(args.state.read_text())
    except Exception:
        state = {}

    def save(result: str) -> None:
        state.update(fingerprint=fp, version=package_version(), result=result,
                     checkedAt=datetime.now().astimezone().isoformat(timespec="seconds"))
        args.state.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.state.with_name(f".{args.state.name}.tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.replace(args.state)

    if args.baseline:
        save("baseline")
        print(f"baseline recorded: {fp}")
        return 0
    if state.get("fingerprint") == fp:
        print(f"stock agents plugin unchanged ({fp})")
        return 0

    version = package_version()
    ok, error, out = dry_port(args.stock)
    if not ok or out is None:
        notify("Agents-plugin: update past niet",
               f"Omarchy heeft het agents-panel bijgewerkt{f' ({version})' if version else ''}, "
               f"maar een aanpassing past niet meer: {error}. Je huidige panel blijft gewoon werken.",
               "critical", not args.no_notify)
        save(f"port-failed: {error}")
        return 0

    try:
        same = all((out / f).read_bytes() == (args.clone / f).read_bytes() for f in COMPARED
                   if (args.clone / f).exists())
    finally:
        shutil.rmtree(out.parent, ignore_errors=True)
    if same:
        print("stock changed, but the port equals the live clone: nothing to take over")
        save("in-sync")
        return 0

    notify("Agents-plugin: update beschikbaar",
           f"Omarchy heeft het agents-panel bijgewerkt{f' ({version})' if version else ''} "
           f"en je aanpassingen passen nog. Overnemen met:\n"
           f"{REPO / 'scripts' / 'apply_stock_port.sh'}",
           "normal", not args.no_notify)
    save("update-available")
    return 0


if __name__ == "__main__":
    sys.exit(main())
