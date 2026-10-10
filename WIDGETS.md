# Widgets

The renderer is **slot-based**. `render()` defines a grid of boxes and hands each to a widget. A widget is just two things:

1. a **fetch** function that returns data (or `None` — fail soft, always), and
2. a **draw** function `w_<name>(d, box, …)` that paints inside `box = (x, y, w, h)`.

Everything scales off the box height, so a widget works at any resolution and in any slot.

## Add a widget in ~20 lines

```python
def fetch_aqi():
    data = _ha_get("/api/states/sensor.air_quality")  # or any source; return None on failure
    return data and data.get("state")

def w_aqi(d, box):
    x, y, w, h = box
    aqi = fetch_aqi()
    d.text((x, y), "AIR", font=_font(True, h * 0.2), fill=0)
    d.text((x, y + h * 0.3), aqi or "—", font=_font(False, h * 0.5), fill=0)
```

Then give it a box in `render()`:

```python
w_aqi(d, (mid, mid_y, rcol, (mid_b - mid_y) * 0.5))
```

Conventions: pure black on white (e-ink is sharpest at 1-bit — `DASH_DARK` inverts the whole frame for you, so always draw dark-on-light); size fonts as a fraction of `h`; truncate text with `_trunc()`; never raise out of a widget (one that raises is logged and skipped, but keep fetches fail-soft anyway).

## Turning widgets on/off

`DASH_WIDGETS` (env) lists which panels render, in order — drop any to hide it, e.g. `DASH_WIDGETS=clock,weather,usage`. No code change. Each name maps to a fixed box in `render()`.

## Data sources — Home Assistant is *one* of them, not the only one

The data widgets (weather, forecast, sun, agenda) currently fetch from **Home Assistant** via the `_ha()` helper — that's the shipped integration, not a requirement of the design. The data layer is just a `fetch_*()` returning a fixed shape; the `w_*()` that draws it doesn't care where the data came from. (The Claude-usage widget already proves this — it's a direct API integration, no HA.)

To add another integration, write a fetch variant for your source and dispatch on an env var, keeping HA as one branch:

```python
WEATHER_PROVIDER = os.environ.get("WEATHER_PROVIDER", "ha")

def fetch_weather():
    if WEATHER_PROVIDER == "open-meteo":
        return _weather_open_meteo()   # lat/lon, no API key
    return _weather_ha()               # the shipped default
```

Good HA-free candidates:
- **Weather + forecast + sun → [Open-Meteo](https://open-meteo.com)** — free, keyless; one GET returns current, daily hi/lo, and sunrise/sunset. Covers three widgets with just a lat/lon.
- **Agenda → an `.ics` URL** — Google/Outlook/Apple all publish one. Recurring events need an ICS library (`icalendar` + recurrence expansion).

Return shapes to match: weather `(condition, temp, feels)` · forecast `[(label, condition, hi, lo), …]` · sun `(sunrise_dt, sunset_dt)` · agenda `[(time_label, summary), …]`. Return `None` on any failure.

## Shipped

Clock · Weather (now) · Forecast (N-day) · Rain (HA hourly forecast, chance per hour) · Agenda (one or more HA calendars) · Sun (HA sunrise/sunset) · Claude usage (5h/7d/Extra) · Codex usage (available windows/Credits) · opencode Go usage (5h/Week/Month).

The three usage providers (`usage`=Claude, `codex`, `opencode`) share one `_usage_block` renderer and split the bottom strip into a column each. Claude and Codex read OAuth usage APIs (refresh-token seeded); **opencode has no usage API, so its widget scrapes the Go dashboard HTML** with a browser cookie — see CONFIGURATION.md for the fragility caveat.

## Roadmap — proposed widgets

| Widget | Source | Effort | Notes |
|---|---|---|---|
| **Now playing** | HA `media_player` / Plex / Jellyfin | low | track/show + artist; hide when idle |
| **Service health** | HTTP/ping checks, or Uptime-Kuma | med | up/down dots for key services |
| **To-do / tasks** | HA `todo.*` / Todoist / a markdown file | low | top N open items |
| **Transit / commute** | GTFS or a transit API | med | next departures for a stop |
| **Markets** | a stocks/crypto API | low | small watchlist, % change |
| **Daily flavor** | bundled list / API | low | quote / word / on-this-day — zero-data charm |
| **Photo / art frame** | a folder of images | med | rotate dithered grayscale art; e-ink loves this |
| **Habit / streak** | a counter file / HA | low | "N days since X" |
| **Air quality / pollen** | HA / AQI API | low | number + trend |
| **Countdown** | a configured date | low | "12 days until …" |
| **Kindle battery** | device-side (`gasgauge`/`lipc`) | low | drawn on-device or POSTed back |

PRs that add a widget should keep it **optional** (no config → hidden) and **fail soft**. If it needs a new dependency, say why in the PR.

## Layout

`render()` is the single place layout lives — a handful of box tuples. Rearrange, resize, or swap widgets there. If you want a fundamentally different grid (e.g. a portrait-native layout, or a rotating multi-screen), that's a `render()` change, not a widget change.
