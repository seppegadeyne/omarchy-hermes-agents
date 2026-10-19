# omarchy-hermes-agents

Show [Hermes Agent](https://hermes-agent.nousresearch.com) token usage, rate
limits, and plan info for every AI coding subscription on the machine, in the
[Omarchy](https://omarchy.org) bar — via a custom collector for the built-in
`omarchy.agents` panel.

One bar icon, one panel per provider: Claude (subscription via the claude
CLI) and ChatGPT, each with live limit meters, reset times, tokens by day,
and tokens by model. The stock Claude / Codex / Fireworks tabs stay disabled.
(Z.AI, Kimi and Nous Research tabs were dropped on 2026-10-14; see git
history for their collectors.)

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
~/.claude/.credentials.json ─┘        (every 5 min, systemd user timer)        │
                                                                       v
                                                     Omarchy agents panel (QML)
```

## Providers

| Provider | Local stats source | Live limits source |
| --- | --- | --- |
| Claude (`claude-cli`) | `billing_provider LIKE 'claude-subscription%'` | `api.anthropic.com/api/oauth/usage` with the claude CLI's own token |
| ChatGPT | `billing_provider in ('openai-codex','auto')` + `chatgpt.com` base URL | `chatgpt.com/backend-api/wham/usage` with Hermes-stored tokens |

Mapping is by billing provider / base URL, so anything routed through Hermes
(its profiles, coding plans, pooled keys) is counted per provider regardless
of which model handled it.

### Auth, read from Hermes at runtime

The collector never stores credentials. At each run it reads:

- `~/.hermes/auth.json` (+ every `profiles/*/auth.json`) — ChatGPT OAuth tokens
- `~/.claude/.credentials.json` — the claude CLI's OAuth token

When a token is missing or idle-expired, the record degrades gracefully: the
panel keeps local token stats and shows a status line instead of meters.

Deliberate safety rules learned the hard way (documented inline):

- **All profile DBs are read, read-only.** Dedicated-profile agents
  (verhuurwinkel, straffesites, ...) log token usage into their own
  `~/.hermes/profiles/<name>/state.db`. Reading only the default DB
  undercounts models that are primarily used there (gpt-6-astra looked
  absent while ~1.66B tokens sat in profile DBs). All DBs are opened
  `mode=ro` because they are live databases owned by running gateways.
- **Collectors never refresh OAuth tokens.** Refresh tokens are single-use
  and rotate on every refresh; the owning client (Hermes' auth layer, the
  claude CLI) owns that rotation. A second refresher races it, replays a
  retired token, and the provider revokes the whole session family.
- **ChatGPT limits fall back to fresh same-account profile tokens.** The
  default profile's openai-codex OAuth chain sits idle when real traffic
  runs from dedicated profiles, so its 10-day access token expires — and
  the lazy "one-shot call refreshes it" recovery fails hard once the
  refresh chain itself is revoked (`refresh_token_reused`, seen 2026-09-23:
  red empty panel frame for days while gpt-* usage kept logging from
  profile agents). The collector gathers access tokens from the default
  auth.json AND every `~/.hermes/profiles/*/auth.json`, pins them to the
  default token's `chatgpt_account_id` (a different-account profile can
  never skew the meter), tries them freshest-JWT-first, and only surfaces
  an auth error when ALL candidates 401. Read-only use; the collector still
  never refreshes openai-codex tokens.
- **The bar icon alarm is pool-aware.** `bindingWindow()` in Panel.qml
  groups meters by their title pool prefix ("Max · …", "Pro · …"), follows
  the roomiest pool while it is below the alarm threshold, and only goes
  red when EVERY pool is ≥ 90% — one maxed-out key in a rotated pool must
  not paint the widget red. Built for the former pooled Z.AI keys; the
  current single-pool providers (claude-cli, chatgpt) have one group, so
  their behavior equals stock.
- **ChatGPT 401 = idle-expired, not broken.** The access JWT has a 10-day
  TTL and Hermes refreshes lazily on the next real use; re-login is only
  needed when the refresh chain itself is revoked.
- **Free plan payload differs from paid plans (handled 2026-10-07).** After
  downgrading ChatGPT to free, every stored access token was 401'd
  server-side (plan switch invalidates sessions even mid-JWT) and the
  wham/usage payload changed shape: `plan_type: "free"`, a single
  `primary_window` of 2592000s (30 days) — which the old ≥168h branch
  mislabeled "Weekly (7-day)" — plus a `credits` object with a balance
  (~62k, unitless) and no cap. Recovery: two one-shot
  `hermes --provider openai-codex --model <supported>` calls let Hermes'
  own auth layer rotate the chain (never refresh from the collector), then
  the collector maps ≥672h windows to "Monthly (30-day)" (Panel.qml's
  windowTitle buckets month/30-day labels as Monthly) and renders the
  credit balance as an info row (`percent: -1`, explicit `title` carrying
  the amount — windowTitle strips parenthesised suffixes, so without a
  title the balance would hide behind the "—" value slot). Panel.qml
  accepts `percent >= -1` in `limitWindows()` (−1 = no meter, "—" value,
  can never win bindingWindow) and hides the Meter bar for info rows.
  Free-plan Codex model support is thin: `gpt-5.5` and `gpt-5.6-luna`
  work, `gpt-5.5-mini`/`gpt-5.6-sol`/`gpt-5.3-codex` return
  400 "model is not supported when using Codex with a ChatGPT account"
  (that 400 also doubles as an auth-OK probe: it proves the token works).
- **Claude (claude-cli provider) reads the claude CLI's own auth, never
  refreshes it.** Usage counts come from the Hermes state DBs
  (`billing_provider LIKE 'claude-subscription%'` — the
  claude-subscription-directsdk plugin spawns the official `claude` CLI as
  a subprocess, and those sessions land under that provider id; the STOCK
  omarchy-agent-usage-claude collector can't see them because it counts
  `~/.claude/projects` transcripts, which the plugin's headless `-p`
  subprocesses never write — so we ship our own tab instead of enabling
  the stock one). Live limits are a READ-ONLY GET on
  `https://api.anthropic.com/api/oauth/usage` with the access token from
  `~/.claude/.credentials.json` (`claudeAiOauth.accessToken`, headers
  mirror the stock collector: `anthropic-beta: oauth-2025-04-20`). The
  claude CLI owns that credential file and rewrites it on its own runs;
  the collector never rotates the token (same rule as the openai-codex
  chain). The payload's percent-scale convention is
  inferred (any utilization ≥ 1 means percent); `five_hour`/`seven_day`
  (or `seven_day_oauth_apps`) become Session/Weekly meters, scoped
  `weekly_scoped` entries become titled meters ("Fable Weekly"), and the
  `extra_usage` credits spend cap (EUR on this account) gets its own
  meter so the panel tracks it before it locks the account. Provider id
  is `claude-cli` (NOT `claude`) so the stock collector, when enabled,
  never races this one writing `usage/claude.json`; the stale stock
  record was removed and `claude` stays disabled in the bar config.
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
omarchy-shell seppe.agents refresh
```

Verify:

```bash
systemctl --user status hermes-usage-collector.timer
ls ~/.local/state/omarchy/agents/usage/   # claude-cli.json, chatgpt.json
journalctl --user -u hermes-usage-collector.service -n 20
```

## Record contract

Each provider is one JSON file in the usage directory. The panel picks up any
file matching this shape (see the collector for the full reference):

```json
{
  "schemaVersion": 1,
  "id": "claude-cli",
  "name": "Claude",
  "ready": true,
  "tierLabel": "Max",
  "limits": [
    { "label": "Session (5h)", "percent": 0.21, "resetsAt": "2026-09-04T07:51:21Z" }
  ],
  "todayPrompts": 58,
  "todaySessions": 4,
  "todayTotalTokens": 2457460,
  "todayTokensByModel": { "claude-opus-5-5": 2457460 },
  "recentDays": [{ "date": "2026-09-04", "messageCount": 2457460 }],
  "modelUsage": {
    "claude-opus-5-5": { "inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0 }
  }
}
```

Key fields: `ready` must be `true` for the tab to appear; `limits[].percent`
is 0.0–1.0; `resetsAt` is ISO-8601; `retryAdvised: true` (optional) asks the
panel for one sooner retry when the limits endpoint was unreachable.

## Widget clone: local patches vs stock

`plugin/` is generated from the installed stock `omarchy.agents` (MIT) by
`scripts/port_from_stock.py`, which applies a small, exact-match patch set.
After an Omarchy update that changes the stock widget, re-port instead of
hand-merging:

```bash
python3 scripts/port_from_stock.py ~/.cache/agents-port
cp ~/.cache/agents-port/{Panel,Main,Agent}.qml ~/.config/omarchy/plugins/seppe.agents/
omarchy-restart-shell   # hot-reload can keep serving the old document
```

A patch that no longer matches aborts the port rather than writing a
half-patched widget. Current patch set (stock r7052, 2026-10-19):

- **IPC target** `seppe.agents`, so `omarchy-shell seppe.agents refresh`
  reaches the clone and not a stock instance.
- **Hermes collector** runs alongside `omarchy-agent-usage-update` on every
  panel refresh / retryAdvised retry (`~/.local/bin/hermes-usage-collector`,
  fixed path; ids are validated before they reach argv).
- **Info rows**: a limit with `percent: -1` (ChatGPT credits) renders as a
  label without meter and never drives the alarm.
- **Pool-aware alarm**: the bar icon follows the roomiest key pool (title
  prefix before "·") and only turns red when every pool is at 90%.
- **Launch action** (right-click) runs the classic Hermes CLI (`hermes --yolo`), not the TUI.
- **No hero buttons and no "Make something cool" tiles**: hidden and removed from the keyboard walk.
- **Panel width** 480 instead of 380.
- **SVG marks** for chatgpt and claude-cli in `assets/`.

Tab order is no longer patched: stock now supports drag/keyboard reordering
and persists it in `~/.local/state/omarchy/agents/order.json`
(set to `["claude-cli","chatgpt"]`). Model names use stock's friendly names.

Full panel documentation: [plugin/PANEL.md](plugin/PANEL.md).

## Repo hygiene

- No secrets are committed: credentials are only ever read at runtime from
  `~/.hermes/`. If you fork this, keep it that way.
- Usage records under `~/.local/state/` never enter the repo.

## License

- Collector and systemd units: [MIT](LICENSE), © Seppe Gadeyne.
- `plugin/` derives from Omarchy (`omarchy.agents`), MIT © Omarchy contributors.
