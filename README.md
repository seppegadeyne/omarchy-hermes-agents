# omarchy-hermes-agents

Show [Hermes Agent](https://hermes-agent.nousresearch.com) token usage, rate
limits, and plan info for every AI coding subscription on the machine, in the
[Omarchy](https://omarchy.org) bar — via a custom collector for the built-in
`omarchy.agents` panel.

One bar icon, one panel per provider: Z.AI, Kimi, ChatGPT, and Nous Research
side by side with the stock Claude / Codex / Fireworks tabs, each with live
limit meters, reset times, tokens by day, and tokens by model.

## How it fits together

The Omarchy agents panel is strictly a display: it watches
`~/.local/state/omarchy/agents/usage/` and draws one tab per JSON record that
appears there, whoever wrote it. This repo adds two pieces on top of that:

1. **`collector/hermes-usage-collector`** — a stdlib-only Python script that
   reads Hermes' SQLite usage database (`~/.hermes/state.db`, table
   `session_model_usage`) and fetches live limits from each provider's API,
   then writes one display-ready record per provider into the usage directory
   (atomic write: temp file + rename, so the panel never reads a half-written
   JSON).
2. **`plugin/`** — a clone of Omarchy's MIT-licensed `omarchy.agents` bar
   widget with local quality-of-life patches (see below). Optional: the stock
   widget already displays custom records fine; the clone adds SVG marks for
   the custom providers and a custom tab order.

```
~/.hermes/state.db ─┐
~/.hermes/auth.json ├─> hermes-usage-collector ─> ~/.local/state/omarchy/agents/usage/*.json
~/.hermes/.env      ─┘         (every 5 min, systemd user timer)        │
                                                                       v
                                                     Omarchy agents panel (QML)
```

## Providers

| Provider | Local stats source | Live limits source |
| --- | --- | --- |
| Z.AI | `billing_provider = 'zai'` (or `auto` + `api.z.ai` base URL) | `api.z.ai/api/monitor/usage/quota/limit` — 5h session + weekly quota |
| Kimi | `billing_provider = 'kimi-coding'` (or `auto` + `api.kimi.com`) | `api.kimi.com/coding/v1/usages` — weekly quota + rate window |
| ChatGPT | `billing_provider in ('openai-codex','auto')` + `chatgpt.com` base URL | `chatgpt.com/backend-api/wham/usage` with Hermes-stored tokens |
| Nous Research | (limits-only in this collector) | `portal.nousresearch.com/api/oauth/account` — credits remaining |

Mapping is by billing provider / base URL, so anything routed through Hermes
(its profiles, coding plans, pooled keys) is counted per provider regardless
of which model handled it.

### Auth, read from Hermes at runtime

The collector never stores credentials. At each run it reads:

- `~/.hermes/auth.json` — OAuth tokens for Nous and ChatGPT, API-key pool
- `~/.hermes/.env` — `KIMI_API_KEY`, `GLM_API_KEY` (or `ZAI_API_KEY`)

When a token is missing or idle-expired, the record degrades gracefully: the
panel keeps local token stats and shows a status line instead of meters.

Two deliberate safety rules learned the hard way (documented inline):

- **Nous tokens are never refreshed from the collector.** Nous refresh tokens
  are single-use and rotate on every refresh; Hermes' own auth layer owns
  that rotation. A second refresher races it, replays a retired token, and
  the portal revokes the whole session family.
- **ChatGPT 401 = idle-expired, not broken.** The access JWT has a 10-day
  TTL and Hermes refreshes lazily on the next real use; re-login is only
  needed when the refresh chain itself is revoked.

## Install

```bash
# 1. Collector
cp collector/hermes-usage-collector ~/.local/bin/
chmod +x ~/.local/bin/hermes-usage-collector

# 2. systemd user timer (every 5 minutes)
cp systemd/hermes-usage-collector.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-usage-collector.timer

# 3. Optional: the patched widget clone
omarchy plugin clone omarchy.agents
# then copy plugin/ over ~/.config/omarchy/plugins/<user>.agents/
cp plugin/*.qml plugin/manifest.json ~/.config/omarchy/plugins/<user>.agents/
mkdir -p ~/.config/omarchy/plugins/<user>.agents/assets
cp plugin/assets/*.svg ~/.config/omarchy/plugins/<user>.agents/assets/

# 4. Refresh the panel
omarchy-shell omarchy.agents refresh
```

Verify:

```bash
systemctl --user status hermes-usage-collector.timer
ls ~/.local/state/omarchy/agents/usage/   # zai.json, kimi.json, chatgpt.json, ...
journalctl --user -u hermes-usage-collector.service -n 20
```

## Record contract

Each provider is one JSON file in the usage directory. The panel picks up any
file matching this shape (see the collector for the full reference):

```json
{
  "schemaVersion": 1,
  "id": "zai",
  "name": "Z.AI",
  "ready": true,
  "tierLabel": "Coding Plan · Max",
  "limits": [
    { "label": "Session (5h) (5856/28000)", "percent": 0.21, "resetsAt": "2026-09-04T07:51:21Z" }
  ],
  "todayPrompts": 58,
  "todaySessions": 4,
  "todayTotalTokens": 2457460,
  "todayTokensByModel": { "glm-5.3": 2457460 },
  "recentDays": [{ "date": "2026-09-04", "messageCount": 2457460 }],
  "modelUsage": {
    "glm-5.3": { "inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0 }
  }
}
```

Key fields: `ready` must be `true` for the tab to appear; `limits[].percent`
is 0.0–1.0; `resetsAt` is ISO-8601; `retryAdvised: true` (optional) asks the
panel for one sooner retry when the limits endpoint was unreachable.

## Widget clone: local patches vs stock

`plugin/` is a clone of Omarchy's `omarchy.agents` (MIT). Differences from
stock, kept small on purpose:

- **Launch action** runs the Hermes TUI (`omarchy-launch-tui --app-id=org.omarchy.agent
  hermes chat --tui --yolo`) instead of the generic agent picker.
- **Tab order** patched in `Main.qml`: `zai` first, then `kimi`, `chatgpt`,
  then alphabetical; stock sorts alphabetically.
- **Model names** render as raw model IDs (stock title-cases them).
- **Panel width** 480 units instead of 380 — the four-way subscription switch
  needs more room.
- **SVG marks** for zai, kimi, nous, chatgpt (+ `zai-light.svg`,
  `codex-light.svg` dark-surface variants) in `assets/`.

Full panel documentation: [plugin/PANEL.md](plugin/PANEL.md).

## Repo hygiene

- No secrets are committed: credentials are only ever read at runtime from
  `~/.hermes/`. If you fork this, keep it that way.
- Usage records under `~/.local/state/` never enter the repo.

## License

- Collector and systemd units: [MIT](LICENSE), © Seppe Gadeyne.
- `plugin/` derives from Omarchy (`omarchy.agents`), MIT © Omarchy contributors.
