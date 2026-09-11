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
   reads Hermes' SQLite usage databases (default `~/.hermes/state.db` plus
   every `~/.hermes/profiles/<name>/state.db`, all opened read-only, table
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
~/.hermes/profiles/*/state.db ─┐
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
- `~/.hermes/.env` — `KIMI_API_KEY`, and every filled Z.AI var
  (`GLM_API_KEY` / `ZAI_API_KEY` / `Z_AI_API_KEY` — one pool key each)

When a token is missing or idle-expired, the record degrades gracefully: the
panel keeps local token stats and shows a status line instead of meters.

Deliberate safety rules learned the hard way (documented inline):

- **All profile DBs are read, read-only.** Dedicated-profile agents
  (verhuurwinkel, straffesites, ...) log token usage into their own
  `~/.hermes/profiles/<name>/state.db`. Reading only the default DB
  undercounts models that are primarily used there (gpt-6-astra looked
  absent while ~1.66B tokens sat in profile DBs). All DBs are opened
  `mode=ro` because they are live databases owned by running gateways.
- **Z.AI quota is fetched per key and grouped by plan LEVEL — never summed
  across levels, never scaled.** Hermes rotates a pool of zai keys, and the
  monitor API reports usage per key/account. Keys on different plans (max +
  pro) have different allowances with independent reset windows: a
  cross-level sum invents a pool that does not exist (the blended percent
  matches no real key — 2502/28000 max + 39723/60000 pro summed to a
  fictional 24% weekly while the pro key was at 66%). Each level gets its
  own meters ("Max · Session (5h)", "Pro · Weekly") with an explicit
  `title` (Panel.qml prefers `title` over label parsing). Keys on the SAME
  level are still summed per window (identical allowances make that an
  honest pool view), with the earliest reset timestamp and joined tier
  label ("Coding Plan · Max + Pro"). A single-key ×2 would have overstated
  allowance (and understated usage) on the mixed-plan pool.
- **Nous tokens are never refreshed from the collector.** Nous refresh tokens
  are single-use and rotate on every refresh; Hermes' own auth layer owns
  that rotation. A second refresher races it, replays a retired token, and
  the portal revokes the whole session family.
- **The bar icon alarm is pool-aware.** `bindingWindow()` in Panel.qml used
  to pick the single fullest meter across the whole provider, so one
  maxed-out key painted the widget red while a sibling key on another plan
  still had plenty of allowance (zai: pro weekly 100% vs max 35% → red,
  even though Hermes happily rotates to the max key). Meters carry the
  pool name in their title prefix ("Max · …", "Pro · …"); the headline now
  follows the roomiest pool's binding window while that pool is below the
  alarm threshold, and only goes red when EVERY pool is ≥ 90%. Single-pool
  providers (kimi, chatgpt, nous) are one pool, so their behavior is
  unchanged.
- **ChatGPT 401 = idle-expired, not broken.** The access JWT has a 10-day
  TTL and Hermes refreshes lazily on the next real use; re-login is only
  needed when the refresh chain itself is revoked.
- **Transient network failures never blank the panel.** A limits fetch that
  fails on DNS/connection/timeout gets one retry after 5s (covers the timer
  run racing system DNS at boot); if it still fails, the last good limits
  are served from a cache (max 2h old, stored *outside* the usage directory
  — the panel renders every `*.json` in there as a tab) with the status
  suffixed "— showing cached limits". Auth errors (invalid key, relogin)
  are never retried and never masked by cache. On top of that the collector
  writes `retryAdvised: true` for transient failures, and this clone runs
  `hermes-usage-collector` alongside `omarchy-agent-usage-update` on every
  panel-triggered refresh — so the panel's built-in 30s retry now also
  covers the custom providers (stock only reruns the built-in collectors).
  The clone registers its IPC under its own target (`seppe.agents`), so
  `omarchy-shell seppe.agents refresh` reaches the clone, not the stock
  panel that loads first.

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
