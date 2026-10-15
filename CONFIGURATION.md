# Configuration

All renderer config is environment variables (in `renderer/.env`; see `.env.example`). Everything except resolution is optional — an unconfigured panel renders a quiet placeholder.

## Display

| Var | Default | Notes |
|---|---|---|
| `KINDLE_W` / `KINDLE_H` | `1024` / `758` | **Landscape** (width ≥ height). Must match your panel — check `cat /sys/class/graphics/fb0/virtual_size` on the device. PW2 = `1024×758`, PW3+/Oasis = `1448×1072`. |
| `TZ` | `UTC` | IANA tz, e.g. `America/Sao_Paulo`. Drives the clock + all formatted times. |
| `DASH_ROTATE` | `0` | `0/90/180/270`. Rotates the frame **server-side** — `90` fills a portrait panel mounted sideways (`270` if upside down). |
| `DASH_DARK` | `0` | `1` = light-on-black (e-ink dark mode; the whole frame is inverted). |
| `DASH_WIDGETS` | all | Comma list of panels to draw, in order: `clock,weather,forecast,agenda,sun,rain,usage`. Drop any to hide it. The legacy name `sunmoon` still works and expands to `sun,rain`. The usage providers (`usage`=Claude, `codex`, `opencode`) share the bottom strip and split it into one column each. |
| `PORT` | `8810` | Host port the container publishes (compose). |
| `DASH_FONT_DIR` | `/usr/share/fonts/truetype/dejavu` | Where the DejaVu TTFs live (the image ships them). |

> **All visual options live here, not on the device.** Rotation, dark mode, and the widget set are container env — the Kindle only ever opens the bare `GET /dash.png`. Change a knob, recreate the container, done; nothing to touch on the Kindle.

## Home Assistant (weather · forecast · rain · sun · agenda)

Optional. Create a long-lived token: HA → Profile → Security → Long-lived access tokens.

| Var | Example | Powers |
|---|---|---|
| `HA_BASE_URL` | `http://192.168.1.5:8123` | all HA panels (blank = all off) |
| `HA_TOKEN` | `eyJ…` | auth |
| `HA_WEATHER_ENTITY` | `weather.home` | weather, the forecast strip, **and** the rain chart |
| `HA_CALENDAR_ENTITY` | `calendar.personal,calendar.work` | agenda (blank = hidden). Comma list: events from every calendar merge into one time-sorted list, and an event on more than one calendar shows once. An event with no title (for example from a calendar shared as free/busy only) shows as `busy`. |
| `HA_SUN_ENTITY` | `sun.sun` | sunrise/sunset |
| `FORECAST_DAYS` | `3` | days in the forecast strip |
| `RAIN_HOURS` | `12` | hours in the rain chart (from the current hour) |
| `RAIN_LAT` / `RAIN_LON` | HA home location | location for the Open-Meteo rain fallback (below) |
| `DASH_MAX_EVENTS` | `4` | agenda rows |
| `DASH_EVENT_GRACE_MIN` | `15` | minutes after its start that a timed event stays on the agenda (an 08:00 event shows until 08:15). An overnight event (`→HH:MM`) hides this long after its end. All-day events stay all day. |

Forecast uses HA's `weather.get_forecasts` service; if your weather integration doesn't support daily forecasts, the strip just hides. The rain chart uses the same service with `type: hourly` and reads `precipitation_probability`. Some integrations (e.g. the default Met.no) give only the rain amount, not the probability; then the chart gets the probability from [Open-Meteo](https://open-meteo.com) instead (free, no API key), at HA's home location or `RAIN_LAT`/`RAIN_LON`. This is the only call that leaves your network for weather. If neither source answers it shows `rain n/a`, and an hour with no probability shows `—`.

## Claude usage (5h / 7d / Extra)

Optional. **Read [SECURITY.md](SECURITY.md) first** — it's an undocumented API and you should give the dashboard its own login.

| Var | Default | Notes |
|---|---|---|
| `CLAUDE_REFRESH_TOKEN` | — | seed from a dedicated login (see SECURITY.md). Blank = panel off. |
| `CLAUDE_POLL_MIN` | `15` | minutes between usage refreshes |
| `CLAUDE_CLIENT_ID` | Claude Code's public id | rarely changes |
| `CLAUDE_TOKEN_URL` | `https://api.anthropic.com/v1/oauth/token` | token refresh endpoint |
| `CLAUDE_USAGE_URL` | `https://api.anthropic.com/api/oauth/usage` | usage endpoint |
| `CLAUDE_STALE_AFTER_MIN` | `45` | after this with no successful fetch, the panel shows `(stale)` |

The renderer shows the five-hour and seven-day plan limits. Its third row shows
monthly Extra Usage spend when enabled. The renderer persists rotated tokens to
`/data/claude_tokens.json` (mount the volume so they survive restarts). The
`CLAUDE_REFRESH_TOKEN` env is only a **seed** used on first run.

## Codex usage (5h / 7d / Credits)

Optional. ChatGPT-subscription quota, via the same OAuth refresh pattern as Claude — give the dashboard its **own** `codex login` (token rotation would otherwise log your workstation out). Add `codex` to `DASH_WIDGETS`.

| Var | Default | Notes |
|---|---|---|
| `CODEX_REFRESH_TOKEN` | — | seed from a dedicated login: `CODEX_HOME=$HOME/dash-codex-auth codex login`, then `jq -r .tokens.refresh_token $HOME/dash-codex-auth/auth.json`. Blank = panel off. |
| `CODEX_POLL_MIN` | `15` | minutes between usage refreshes |
| `CODEX_ACCOUNT_ID` | — | the `ChatGPT-Account-Id` header; auto-read from the id_token JWT, set only to override |
| `CODEX_CLIENT_ID` | Codex CLI's public id | rarely changes |
| `CODEX_TOKEN_URL` | `https://auth.openai.com/oauth/token` | token refresh endpoint |
| `CODEX_USAGE_URL` | `https://chatgpt.com/backend-api/wham/usage` | usage endpoint |

Reads the included-usage windows plus the purchased credit balance returned by
the usage endpoint. Window labels come from their durations, not their position:
five hours is shown as `5h`, seven days as `7d`, and absent windows are hidden.
Included usage is consumed first; credits cover eligible usage after a plan
limit is reached. Rotated tokens persist to `/data/codex_tokens.json`;
`CLAUDE_STALE_AFTER_MIN` also drives this panel's `(stale)` marker.

## opencode Go usage (5h / Week / Month)

Optional, and **fragile**: opencode has no usage API (tracked upstream — [Go #16017](https://github.com/anomalyco/opencode/issues/16017)), so this **scrapes the Go dashboard HTML** with your browser session cookie. The cookie expires (days) with no refresh path — when it dies the panel shows `(stale)` until you re-seed it. Add `opencode` to `DASH_WIDGETS`.

| Var | Default | Notes |
|---|---|---|
| `OPENCODE_WORKSPACE_ID` | — | the `wrk_…` in the dashboard URL `/workspace/<id>/go`. Blank = panel off. |
| `OPENCODE_AUTH_COOKIE` | — | the `auth` cookie for `opencode.ai` (DevTools → Application → Cookies). Re-seed when it expires. |
| `OPENCODE_POLL_MIN` | `15` | minutes between scrapes |
| `OPENCODE_GO_URL` | `https://opencode.ai/workspace/{ws}/go` | dashboard URL template (`{ws}` = workspace id) |

Parses the `rollingUsage` / `weeklyUsage` / `monthlyUsage` objects (`usagePercent` + `resetInSec`) embedded in the page. If opencode changes the dashboard markup this breaks — the panel then renders `(stale)`/`usage n/a`, never a broken frame.

## Device (`dashboard.sh`)

| Var | Default | Notes |
|---|---|---|
| `DASH_URL` | `http://CHANGE-ME.lan/dash.png?rotate=90` | where the device fetches; set to your renderer |
| `DASH_PROXY` | — | proxy for the fetch. Set to `http://127.0.0.1:1056` (the KOReader Tailscale plugin's proxy) to fetch over your tailnet, e.g. `DASH_URL=http://servarr:8810/dash.png`. Also starts `tailscaled` if it isn't running. Blank = plain LAN fetch. See [INSTALLATION.md](INSTALLATION.md#3-away-from-home-tailscale). |
| `TS_BIN` | `/mnt/us/koreader/plugins/tailscale.koplugin/bin` | where the plugin keeps `tailscaled` and its state; only used when `DASH_PROXY` is set |
| `FULL_EVERY` | `30` | full (flashing) de-ghost refresh every N minute-cycles |
| `LOG` | `/tmp/kindle-dash.log` | script log (kept off-screen) |
