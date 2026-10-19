#!/usr/bin/env python3
"""Rebuild the seppe.agents clone from the installed stock omarchy.agents plugin.

Applies our local patches on top of /usr/share/omarchy/shell/plugins/agents so
an Omarchy update can be followed with one command instead of a hand merge.
Every replacement must match exactly once; a miss aborts without writing.

Usage: python3 scripts/port_from_stock.py OUT_DIR [--stock DIR]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

STOCK = Path("/usr/share/omarchy/shell/plugins/agents")


def sub(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"patch '{label}': expected 1 match, found {count}")
    return text.replace(old, new)


def patch_panel(t: str) -> str:
    t = sub(t, '  ipcTarget: "omarchy.agents"\n',
            '  // Own IPC target: a stock omarchy.agents instance would otherwise\n'
            '  // register first and swallow refresh calls meant for this clone.\n'
            '  ipcTarget: "seppe.agents"\n', "ipc-target")

    # One alarm threshold shared by the bar icon and the pool-aware headline.
    t = sub(t, "  readonly property bool alarming: {\n",
            "  readonly property real alarmThreshold: 0.9\n"
            "  readonly property bool alarming: {\n", "alarm-threshold")
    t = sub(t, "      if (window && window.percent >= 0.9) return true\n",
            "      if (window && window.percent >= root.alarmThreshold) return true\n", "alarm-use")

    t = sub(t, '    if (root.bar) root.bar.run("omarchy-agent --pick")\n',
            '    if (root.bar) root.bar.run("omarchy-launch-tui --app-id=org.omarchy.agent hermes chat --tui --yolo")\n',
            "launch-hermes")

    # percent -1 is an info row (e.g. a credit balance without a cap).
    t = sub(t, "      if (percent >= 0) out.push(limitWindow(entry.label, percent, entry.resetsAt, entry.title))\n",
            "      // -1 is the info-row sentinel (an uncapped credit balance): shown\n"
            "      // without a meter, and it never wins bindingWindow.\n"
            "      if (percent >= -1) out.push(limitWindow(entry.label, percent, entry.resetsAt, entry.title))\n",
            "info-rows")

    old_binding = (
        "  function bindingWindow(p) {\n"
        "    var windows = limitWindows(p)\n"
        "    var best = null\n"
        "    for (var i = 0; i < windows.length; i++) {\n"
        "      if (!best || windows[i].percent > best.percent) best = windows[i]\n"
        "    }\n"
        "    return best\n"
        "  }\n")
    new_binding = (
        "  //\n"
        "  // Pooled credentials (one provider rotating keys on different plans)\n"
        "  // have one binding window per pool, named by the title prefix before\n"
        "  // \"·\". The headline follows the roomiest pool while any pool can still\n"
        "  // take prompts, so one exhausted key does not turn the icon red.\n"
        "  function poolNameForTitle(title) {\n"
        "    var text = String(title || \"\")\n"
        "    var cut = text.indexOf(\"·\")\n"
        "    return cut > 0 ? text.substring(0, cut).trim() : \"\"\n"
        "  }\n"
        "\n"
        "  function bindingWindow(p) {\n"
        "    var windows = limitWindows(p)\n"
        "    var fullest = null\n"
        "    var poolWindows = ({})\n"
        "    var poolOrder = []\n"
        "    for (var i = 0; i < windows.length; i++) {\n"
        "      var w = windows[i]\n"
        "      if (w.percent < 0) continue\n"
        "      if (!fullest || w.percent > fullest.percent) fullest = w\n"
        "      var pool = poolNameForTitle(w.title)\n"
        "      if (!Object.prototype.hasOwnProperty.call(poolWindows, pool)) { poolWindows[pool] = null; poolOrder.push(pool) }\n"
        "      if (!poolWindows[pool] || w.percent > poolWindows[pool].percent) poolWindows[pool] = w\n"
        "    }\n"
        "    var roomiest = null\n"
        "    for (var j = 0; j < poolOrder.length; j++) {\n"
        "      var worst = poolWindows[poolOrder[j]]\n"
        "      if (worst && (!roomiest || worst.percent < roomiest.percent)) roomiest = worst\n"
        "    }\n"
        "    if (roomiest && roomiest.percent < alarmThreshold) return roomiest\n"
        "    return fullest\n"
        "  }\n")
    t = sub(t, old_binding, new_binding, "pool-binding")

    # CompactLimit: info rows get no meter, no alarm and no "-100%" tooltip.
    t = sub(t, "    readonly property bool alarming: window && window.percent >= 0.9\n",
            "    readonly property bool info: !!window && window.percent < 0\n"
            "    readonly property bool alarming: !!window && window.percent >= root.alarmThreshold\n",
            "compact-info")
    t = sub(t,
            "          lines.push(compact.resetMs > 0\n",
            "          if (compact.info) lines.push(windowName)\n"
            "          else lines.push(compact.resetMs > 0\n",
            "compact-tooltip")
    t = sub(t,
            "      value: compact.window ? compact.window.percent : -1\n"
            "      alarming: compact.alarming\n",
            "      visible: !compact.info\n"
            "      value: compact.window ? compact.window.percent : -1\n"
            "      alarming: compact.alarming\n",
            "compact-meter")

    t = sub(t, "    contentWidth: panel.fittedContentWidth(Style.space(380))\n",
            "    contentWidth: panel.fittedContentWidth(Style.space(480))\n", "panel-width")
    return t


def patch_main(t: str) -> str:
    t = sub(t,
            "  function updateCommand(kind, agentIds) {\n",
            "  // Hermes-managed providers (claude-cli, chatgpt) come from\n"
            "  // hermes-usage-collector, which the stock orchestrator never runs. It\n"
            "  // speaks the same flags, so panel refreshes and the 30s retry reach it.\n"
            "  readonly property var hermesProviderIds: [\"claude-cli\", \"chatgpt\"]\n"
            "  readonly property string hermesCollectorBin: root.home !== \"\" ? root.home + \"/.local/bin/hermes-usage-collector\" : \"\"\n"
            "\n"
            "  Process {\n"
            "    id: hermesProcess\n"
            "    running: false\n"
            "    onExited: {\n"
            "      root.rescanAgents()\n"
            "      root.reloadRecords()\n"
            "    }\n"
            "\n"
            "    stderr: StdioCollector {\n"
            "      waitForEnd: true\n"
            "      onStreamFinished: if (text.trim() !== \"\") console.warn(\"agents-hermes\", text.trim().slice(0, 2000))\n"
            "    }\n"
            "  }\n"
            "\n"
            "  function hermesCollectorCommand(kind, agentIds) {\n"
            "    var command = [hermesCollectorBin]\n"
            "    if (kind === \"force\") command.push(\"--force\")\n"
            "    if (kind === \"limits\") command.push(\"--limits-only\")\n"
            "    var providers = settings && settings.providers ? settings.providers : {}\n"
            "    for (var id in providers) {\n"
            "      if (providers[id] && providers[id].enabled === false && /^[a-z0-9][a-z0-9-]*$/.test(id)) command.push(\"--except\", id)\n"
            "    }\n"
            "    if (agentIds) {\n"
            "      for (var i = 0; i < agentIds.length; i++)\n"
            "        if (/^[a-z0-9][a-z0-9-]*$/.test(String(agentIds[i]))) command.push(String(agentIds[i]))\n"
            "    }\n"
            "    return command\n"
            "  }\n"
            "\n"
            "  function hasHermesProviders(agentIds) {\n"
            "    var ids = agentIds && agentIds.length > 0 ? agentIds : root.agentIds\n"
            "    for (var i = 0; i < ids.length; i++)\n"
            "      if (hermesProviderIds.indexOf(String(ids[i])) !== -1) return true\n"
            "    return false\n"
            "  }\n"
            "\n"
            "  function runHermesCollector(kind, agentIds) {\n"
            "    if (hermesCollectorBin === \"\" || hermesProcess.running || !hasHermesProviders(agentIds)) return\n"
            "    hermesProcess.command = hermesCollectorCommand(kind, agentIds)\n"
            "    hermesProcess.running = true\n"
            "  }\n"
            "\n"
            "  function updateCommand(kind, agentIds) {\n",
            "hermes-process")
    t = sub(t,
            "    updateProcess.command = updateCommand(kind, agentIds)\n"
            "    updateProcess.running = true\n",
            "    updateProcess.command = updateCommand(kind, agentIds)\n"
            "    updateProcess.running = true\n"
            "    runHermesCollector(kind, agentIds)\n",
            "hermes-run")
    return t


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--stock", type=Path, default=STOCK)
    args = parser.parse_args()
    stock = args.stock
    panel = patch_panel((stock / "Panel.qml").read_text(encoding="utf-8"))
    main_qml = patch_main((stock / "Main.qml").read_text(encoding="utf-8"))
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "Panel.qml").write_text(panel, encoding="utf-8")
    (out / "Main.qml").write_text(main_qml, encoding="utf-8")
    shutil.copyfile(stock / "Agent.qml", out / "Agent.qml")
    shutil.copyfile(stock / "README.md", out / "README.md")
    (out / "assets").mkdir(exist_ok=True)
    for svg in (stock / "assets").glob("*.svg"):
        shutil.copyfile(svg, out / "assets" / svg.name)
    print(f"ported {stock} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
