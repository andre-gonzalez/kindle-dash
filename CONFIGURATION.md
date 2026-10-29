# Configuration

All renderer config is environment variables (in `renderer/.env`; see `.env.example`). Everything except resolution is optional — an unconfigured panel renders a quiet placeholder.

## Display

| Var | Default | Notes |
|---|---|---|
| `KINDLE_W` / `KINDLE_H` | `1024` / `758` | **Landscape** (width ≥ height). Must match your panel — check `cat /sys/class/graphics/fb0/virtual_size` on the device. PW2 = `1024×758`, PW3+/Oasis = `1448×1072`. |
| `TZ` | `UTC` | IANA tz of the **home** location, e.g. `America/Sao_Paulo`. Drives the clock + all formatted times at home; away from home they follow the current location's zone (see [Location](#location)). |
| `DASH_ROTATE` | `0` | `0/90/180/270`. Rotates the frame **server-side** — `90` fills a portrait panel mounted sideways (`270` if upside down). |
| `DASH_DARK` | `0` | `1` = light-on-black (e-ink dark mode; the whole frame is inverted). |
| `DASH_WIDGETS` | all | Comma list of panels to draw, in order: `clock,weather,forecast,agenda,sun,rain,usage,battery`. Drop any to hide it. The legacy name `sunmoon` still works and expands to `sun,rain`. The usage providers (`usage`=Claude, `codex`, `opencode`, `openrouter`) share the bottom strip and split it into one column each. `battery` shows the Kindle's charge in the top-right corner; it needs no config — `dashboard.sh` sends `?battery=&charging=` with each fetch, and the panel stays blank for any other client (e.g. a browser). |
| `PORT` | `8810` | Host port the container publishes (compose). |
| `DASH_FONT_DIR` | `/usr/share/fonts/truetype/dejavu` | Where the DejaVu TTFs live (the image ships them). |

> **All visual options live here, not on the device.** Rotation, dark mode, and the widget set are container env — the Kindle only ever opens the bare `GET /dash.png`. Change a knob, recreate the container, done; nothing to touch on the Kindle.

## Weather (weather · forecast · rain · sun)

From [Open-Meteo](https://open-meteo.com): free, no API key, no account. One request each `DASH_POLL_MIN` gets the current weather, the daily forecast, the hourly rain chance, and the sunrise/sunset times. It sends only the coordinates below. If the request fails, the panels show their placeholders (`weather n/a`, `rain n/a`), and an hour with no rain chance shows `—`.

| Var | Default | Notes |
|---|---|---|
| `DASH_LAT` / `DASH_LON` | blank | location, decimal degrees (e.g. `-23.55` / `-46.63`). Blank = weather, forecast, rain, and sun are off. `RAIN_LAT` / `RAIN_LON` are the old names and still work. |
| `DASH_UNITS` | `metric` | `metric` (°C) or `imperial` (°F) |
| `FORECAST_DAYS` | `3` | days in the forecast strip, from today |
| `RAIN_HOURS` | `12` | hours in the rain chart (from the current hour) |
| `DASH_POLL_MIN` | `5` | minutes between weather and agenda refreshes |
| `OPEN_METEO_URL` | `https://api.open-meteo.com/v1/forecast` | override only for a self-hosted Open-Meteo |

Conditions use the Home Assistant names (`sunny`, `clear-night`, `partlycloudy`, `cloudy`, `fog`, `rainy`, `pouring`, `snowy`, `snowy-rainy`, `lightning`, `lightning-rainy`), mapped from Open-Meteo's WMO weather codes.

> **Moving from Home Assistant weather:** `HA_WEATHER_ENTITY` and `HA_SUN_ENTITY` are no longer used. If one is set, the renderer logs a warning at start and ignores it. Set `DASH_LAT`/`DASH_LON` to your home coordinates (HA → Settings → System → General shows them).

## Location

The weather, the time zone, and a small city line under the weather follow the **current location**. The renderer selects the first one that exists:

1. a **manual override**;
2. the **geo-IP** location of the Kindle;
3. the **last known** geo-IP location (so one failed lookup does not jump home);
4. **home**: `DASH_LAT`/`DASH_LON` with `TZ`.

Away from home, the clock, the agenda day, the sun times, the rain hours, and the usage reset times use the location's time zone. The city line shows the location name, with `(manual)` for an override. At home there is no city line, so the home dashboard looks the same as before.

Set or clear the override on the [`/location` page](USAGE.md#set-the-location-by-hand-travel). Its city search uses the [Open-Meteo geocoding API](https://open-meteo.com/en/docs/geocoding-api) (free, no key; `GEOCODING_URL` overrides the endpoint). The override and the last known location persist in `location.json` in the state dir (`/data`), so a restart keeps them.

## Agenda (ICS calendars · Home Assistant)

Optional. The agenda reads ICS calendar URLs, Home Assistant calendars, or both. Events from every calendar merge into one time-sorted list, and an event on more than one calendar shows once. An event with no title (for example from a calendar shared as free/busy only) shows as `busy`. If one calendar fails, the others still show; if all fail, the agenda shows `no calendar`.

| Var | Example | Powers |
|---|---|---|
| `DASH_ICS_URLS` | `https://calendar.google.com/calendar/ical/…/basic.ics` | comma list of iCal URLs. Google: Calendar settings → *Integrate calendar* → *Secret address in iCal format*. Outlook, iCloud, and Nextcloud publish one too. Repeat events, exceptions, and moved instances expand; cancelled events hide. **Each URL is a secret** — see [SECURITY.md](SECURITY.md#ics-calendar-urls-are-secrets). |
| `HA_BASE_URL` | `http://192.168.1.5:8123` | Home Assistant calendars (blank = off). Create a long-lived token: HA → Profile → Security → Long-lived access tokens. |
| `HA_TOKEN` | `eyJ…` | auth |
| `HA_CALENDAR_ENTITY` | `calendar.personal,calendar.work` | comma list of HA calendar entities |
| `DASH_MAX_EVENTS` | `4` | agenda rows |
| `DASH_EVENT_GRACE_MIN` | `15` | minutes after its start that a timed event stays on the agenda (an 08:00 event shows until 08:15). An overnight event (`→HH:MM`) hides this long after its end. All-day events stay all day. |

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

## OpenRouter monthly spend (Month USD)

Optional. Shows how many USD you spent on OpenRouter this month, across **all** API keys. Add `openrouter` to `DASH_WIDGETS`.

| Var | Default | Notes |
|---|---|---|
| `OPENROUTER_MANAGEMENT_KEY` | — | a **management** key (openrouter.ai/settings/management-keys). A normal API key can't list keys. Blank = panel off. |
| `OPENROUTER_MONTH_BUDGET` | — | monthly budget in USD. The bar shows spend / budget and stops at 100%; the text always shows the real spend (`$34.10 / $30`). Blank = USD text only, empty bar. |
| `OPENROUTER_POLL_MIN` | `15` | minutes between usage refreshes |
| `OPENROUTER_KEYS_URL` | `https://openrouter.ai/api/v1/keys` | key list endpoint |

Lists every key (disabled and expired included) and adds up `usage_monthly` — OpenRouter credit spend for the current **UTC** month, so the reset shows as 00:00 UTC on the 1st in your `TZ`. Limits:

- **Deleted keys** drop out of the list, so their spend this month drops out of the total.
- **Default workspace only** — keys in other workspaces aren't counted.
- **BYOK** spend (paid to your own provider account) isn't counted; the total matches your OpenRouter credits.

`CLAUDE_STALE_AFTER_MIN` also drives this panel's `(stale)` marker.

## Device (`dashboard.sh`)

| Var | Default | Notes |
|---|---|---|
| `DASH_URL` | `http://CHANGE-ME.lan/dash.png?rotate=90` | where the device fetches; set to your renderer |
| `DASH_PROXY` | — | proxy for the fetch. Set to `http://127.0.0.1:1056` (the KOReader Tailscale plugin's proxy) to fetch over your tailnet, e.g. `DASH_URL=http://servarr:8810/dash.png`. Also starts `tailscaled` if it isn't running. Blank = plain LAN fetch. See [INSTALLATION.md](INSTALLATION.md#3-away-from-home-tailscale). |
| `TS_BIN` | `/mnt/us/koreader/plugins/tailscale.koplugin/bin` | where the plugin keeps `tailscaled` and its state; only used when `DASH_PROXY` is set |
| `FULL_EVERY` | `30` | full (flashing) de-ghost refresh every N minute-cycles |
| `LOG` | `/tmp/kindle-dash.log` | script log (kept off-screen) |
