#!/usr/bin/env python3
"""kindle-dash — render a grayscale dashboard PNG for a jailbroken Kindle.

A small HTTP server that renders a dashboard image sized to a Kindle's e-ink
screen. The Kindle fetches GET /dash.png on a timer and paints it with eips.

Endpoints:
  GET /dash.png[?rotate=90]   Rendered dashboard (grayscale PNG). rotate spins
                              the image so a landscape design fills a portrait
                              framebuffer when the device is mounted sideways.
     [&battery=82&charging=1] The device's battery, for the battery widget
                              (dashboard.sh sends it on every fetch).
  GET /healthz                Liveness probe.

Layout is slot-based: render() lays out a grid of boxes and hands each to a
widget — a function `w_<name>(draw, box, ...)` that draws inside its box. Add a
widget by writing a fetch + a w_ function and giving it a box (see WIDGETS.md).

Everything is config via env (see CONFIGURATION.md / .env.example). All external
fetches fail soft: a missing source renders a placeholder, never a broken image.
"""
import base64
import functools
import html
import json
import math
import os
import re
import threading
import time
import zoneinfo
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

# --- config ----------------------------------------------------------------
KINDLE_W = int(os.environ.get("KINDLE_W", "1024"))
KINDLE_H = int(os.environ.get("KINDLE_H", "758"))
TZ = os.environ.get("TZ", "UTC")
TZINFO = zoneinfo.ZoneInfo(TZ)

# Visual options are server-side (container env) so the device just opens
# /dash.png with no query string. DASH_ROTATE spins the frame for sideways
# mounting (0/90/180/270); DASH_DARK inverts to light-on-black; DASH_WIDGETS
# picks which panels render, in order.
DASH_ROTATE = int(os.environ.get("DASH_ROTATE", "0"))
DASH_DARK = os.environ.get("DASH_DARK", "0").lower() in ("1", "true", "yes", "on")
DASH_WIDGETS = [
    w.strip()
    for w in os.environ.get("DASH_WIDGETS", "clock,weather,forecast,agenda,sun,rain,usage,battery").split(",")
    if w.strip()
]
# The old "sunmoon" panel became "sun" (sunrise/sunset only) + "rain" (hourly
# rain chance); expand the legacy name so existing configs keep working.
DASH_WIDGETS = [n for w in DASH_WIDGETS for n in (("sun", "rain") if w == "sunmoon" else (w,))]

# Home Assistant (optional) — powers weather, forecast, rain, sun, and agenda widgets.
HA_BASE_URL = os.environ.get("HA_BASE_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_WEATHER_ENTITY = os.environ.get("HA_WEATHER_ENTITY", "")
# Comma list: the agenda merges every calendar into one time-sorted list.
HA_CALENDAR_ENTITIES = [
    c.strip() for c in os.environ.get("HA_CALENDAR_ENTITY", "").split(",") if c.strip()
]
HA_SUN_ENTITY = os.environ.get("HA_SUN_ENTITY", "sun.sun")
FORECAST_DAYS = int(os.environ.get("FORECAST_DAYS", "3"))
RAIN_HOURS = int(os.environ.get("RAIN_HOURS", "12"))  # columns in the rain chart
# Rain chance falls back to Open-Meteo (free, no key) when the HA weather entity
# has no hourly precipitation_probability (e.g. Met.no). The location defaults
# to HA's home coordinates; RAIN_LAT/RAIN_LON override it.
OPEN_METEO_URL = os.environ.get("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast")
RAIN_LAT = os.environ.get("RAIN_LAT", "")
RAIN_LON = os.environ.get("RAIN_LON", "")
MAX_EVENTS = int(os.environ.get("DASH_MAX_EVENTS", "4"))
EVENT_GRACE_MIN = int(os.environ.get("DASH_EVENT_GRACE_MIN", "15"))  # past-event hide delay
DASH_POLL_MIN = int(os.environ.get("DASH_POLL_MIN", "5"))  # HA refresh cadence

# Claude usage (optional) — fetched from the OAuth usage API; the short-lived
# access token is refreshed from a seeded refresh token (its OWN login — see
# SECURITY.md). Tokens persist to TOKEN_FILE so rotations survive restarts.
CLAUDE_REFRESH_TOKEN = os.environ.get("CLAUDE_REFRESH_TOKEN", "")
CLAUDE_CLIENT_ID = os.environ.get("CLAUDE_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e")
CLAUDE_TOKEN_URL = os.environ.get("CLAUDE_TOKEN_URL", "https://api.anthropic.com/v1/oauth/token")
CLAUDE_USAGE_URL = os.environ.get("CLAUDE_USAGE_URL", "https://api.anthropic.com/api/oauth/usage")
# The usage endpoint puts requests with no claude-code-style User-Agent into an
# aggressively rate-limited bucket (persistent 429s). Send one.
CLAUDE_USER_AGENT = os.environ.get("CLAUDE_USER_AGENT", "claude-cli/2.1.179 (external, cli)")
CLAUDE_POLL_MIN = int(os.environ.get("CLAUDE_POLL_MIN", "15"))

STATE_DIR = os.environ.get("DASH_STATE_DIR", "/data")
STALE_AFTER_MIN = int(os.environ.get("CLAUDE_STALE_AFTER_MIN", "45"))
USAGE_FILE = os.path.join(STATE_DIR, "claude_usage.json")
TOKEN_FILE = os.path.join(STATE_DIR, "claude_tokens.json")
FONT_DIR = os.environ.get("DASH_FONT_DIR", "/usr/share/fonts/truetype/dejavu")

# Codex usage (optional) — ChatGPT-subscription quota via the same OAuth dance
# as Claude: a seeded refresh token (its OWN `codex login`, see SECURITY.md)
# mints short-lived access tokens, which rotate and persist to CODEX_TOKEN_FILE.
# account_id (the ChatGPT-Account-Id header) is read from the id_token JWT.
CODEX_REFRESH_TOKEN = os.environ.get("CODEX_REFRESH_TOKEN", "")
CODEX_CLIENT_ID = os.environ.get("CODEX_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann")
CODEX_TOKEN_URL = os.environ.get("CODEX_TOKEN_URL", "https://auth.openai.com/oauth/token")
CODEX_USAGE_URL = os.environ.get("CODEX_USAGE_URL", "https://chatgpt.com/backend-api/wham/usage")
CODEX_ACCOUNT_ID = os.environ.get("CODEX_ACCOUNT_ID", "")  # optional override
CODEX_USER_AGENT = os.environ.get("CODEX_USER_AGENT", "codex_cli_rs/0.20.0")
CODEX_POLL_MIN = int(os.environ.get("CODEX_POLL_MIN", "15"))
CODEX_USAGE_FILE = os.path.join(STATE_DIR, "codex_usage.json")
CODEX_TOKEN_FILE = os.path.join(STATE_DIR, "codex_tokens.json")

# opencode Go usage (optional) — opencode exposes NO usage API (see WIDGETS.md),
# so this scrapes the Go dashboard HTML, authenticated by a browser `auth`
# cookie. The cookie expires (days) with no refresh path: when it dies the
# widget renders "(stale)" until OPENCODE_AUTH_COOKIE is re-seeded.
OPENCODE_WORKSPACE_ID = os.environ.get("OPENCODE_WORKSPACE_ID", "")
OPENCODE_AUTH_COOKIE = os.environ.get("OPENCODE_AUTH_COOKIE", "")
OPENCODE_GO_URL = os.environ.get("OPENCODE_GO_URL", "https://opencode.ai/workspace/{ws}/go")
OPENCODE_USER_AGENT = os.environ.get(
    "OPENCODE_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
)
OPENCODE_POLL_MIN = int(os.environ.get("OPENCODE_POLL_MIN", "15"))
OPENCODE_USAGE_FILE = os.path.join(STATE_DIR, "opencode_usage.json")

# OpenRouter monthly spend (optional) — sums usage_monthly (USD, current UTC
# month) over every key in the default workspace. Listing keys needs a
# MANAGEMENT key (it can also create/delete keys — see SECURITY.md). Keys
# deleted this month drop out of the sum. BYOK spend is not included.
OPENROUTER_MANAGEMENT_KEY = os.environ.get("OPENROUTER_MANAGEMENT_KEY", "")
OPENROUTER_KEYS_URL = os.environ.get("OPENROUTER_KEYS_URL", "https://openrouter.ai/api/v1/keys")
OPENROUTER_MONTH_BUDGET = float(os.environ.get("OPENROUTER_MONTH_BUDGET") or 0) or None
OPENROUTER_POLL_MIN = int(os.environ.get("OPENROUTER_POLL_MIN", "15"))
OPENROUTER_USAGE_FILE = os.path.join(STATE_DIR, "openrouter_usage.json")


@functools.lru_cache(maxsize=None)
def _font(bold, size):
    # Fonts are immutable for the process; cache so we don't re-read+parse the
    # TTF on every text call (render() asks for ~25 fonts a frame).
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(os.path.join(FONT_DIR, name), int(size))


# --- Home Assistant helpers ------------------------------------------------
def _ha(method, path, body=None, **params):
    if not (HA_BASE_URL and HA_TOKEN):
        return None
    try:
        r = requests.request(
            method,
            f"{HA_BASE_URL}{path}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            params=params or None,
            json=body,
            timeout=8,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[ha] {method} {path} failed: {e}", flush=True)
        return None


def _local(iso):
    """Parse an ISO timestamp to local tz, or None."""
    try:
        return datetime.fromisoformat(iso).astimezone(TZINFO)
    except Exception:
        return None


def fetch_weather():
    """(condition, temp, feels) or (None, None, None)."""
    data = _ha("GET", f"/api/states/{HA_WEATHER_ENTITY}") if HA_WEATHER_ENTITY else None
    if not data:
        return None, None, None
    a = data.get("attributes", {})
    return data.get("state"), a.get("temperature"), a.get("apparent_temperature")


def fetch_forecast():
    """List of (label, condition, hi, lo) for the next days, or None."""
    if not HA_WEATHER_ENTITY:
        return None
    resp = _ha(
        "POST",
        "/api/services/weather/get_forecasts",
        {"entity_id": HA_WEATHER_ENTITY, "type": "daily"},
        return_response="true",
    )
    try:
        days = resp["service_response"][HA_WEATHER_ENTITY]["forecast"]
    except Exception:
        return None
    out = []
    for f in days[:FORECAST_DAYS]:
        dt = _local(f.get("datetime"))
        out.append((dt.strftime("%a") if dt else "", f.get("condition"),
                    f.get("temperature"), f.get("templow")))
    return out or None


def parse_hourly(resp, entity):
    """[(local_dt, rain_pct-or-None)] from a get_forecasts hourly response, or None."""
    try:
        hours = resp["service_response"][entity]["forecast"]
    except Exception:
        return None
    out = []
    for f in hours:
        dt = _local(f.get("datetime"))
        if dt is None:
            continue
        pct = f.get("precipitation_probability")
        out.append((dt, round(pct) if isinstance(pct, (int, float)) else None))
    return out or None


def parse_open_meteo(data):
    """[(local_dt, rain_pct-or-None)] from an Open-Meteo unixtime response, or None."""
    try:
        h = data["hourly"]
        pairs = list(zip(h["time"], h["precipitation_probability"]))
    except Exception:
        return None
    out = [
        (datetime.fromtimestamp(t, TZINFO), round(p) if isinstance(p, (int, float)) else None)
        for t, p in pairs
    ]
    return out or None


def fetch_open_meteo():
    """Hourly rain probability from Open-Meteo (see parse_open_meteo), or None."""
    if RAIN_LAT and RAIN_LON:
        lat, lon = RAIN_LAT, RAIN_LON
    else:
        cfg = _ha("GET", "/api/config") or {}
        lat, lon = cfg.get("latitude"), cfg.get("longitude")
    if lat is None or lon is None:
        return None
    try:
        r = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "precipitation_probability", "timeformat": "unixtime",
                "forecast_days": 1 + -(-RAIN_HOURS // 24),  # today + enough to cover RAIN_HOURS
            },
            timeout=8,
        )
        r.raise_for_status()
        return parse_open_meteo(r.json())
    except Exception as e:
        print(f"[open-meteo] fetch failed: {e}", flush=True)
        return None


def fetch_hourly():
    """Hourly rain probability: the HA weather entity, else Open-Meteo. Or None."""
    hours = None
    if HA_WEATHER_ENTITY:
        resp = _ha(
            "POST",
            "/api/services/weather/get_forecasts",
            {"entity_id": HA_WEATHER_ENTITY, "type": "hourly"},
            return_response="true",
        )
        hours = parse_hourly(resp, HA_WEATHER_ENTITY)
    if hours and any(pct is not None for _, pct in hours):
        return hours
    return fetch_open_meteo() or hours


def fetch_sun():
    """(sunrise_dt, sunset_dt) in local tz, or (None, None)."""
    data = _ha("GET", f"/api/states/{HA_SUN_ENTITY}") if HA_SUN_ENTITY else None
    if not data:
        return None, None
    a = data.get("attributes", {})
    return _local(a.get("next_rising")), _local(a.get("next_setting"))


def parse_events(calendars, start):
    """Merge HA calendar event lists into [(shown_dt, HH:MM-or-'all day', summary)].

    Time-sorted, all-day events first. An event that is on more than one
    calendar (e.g. a meeting invite) shows once. A timed event that began
    before today (e.g. overnight) shows as '→HH:MM' (its end) with the all-day ones.
    An event with no title (e.g. from a free/busy-only share) shows as 'busy'.
    shown_dt is the time the label shows (None for 'all day'); visible_events()
    uses it to hide events that have passed.
    """
    out = {}
    for ev in (ev for data in calendars for ev in data):
        so = ev.get("start", {})
        summary = ev.get("summary") or "busy"  # free/busy-only shares have no title
        dt = _local(so.get("dateTime")) if "dateTime" in so else None
        if dt and dt < start:
            end = _local(ev.get("end", {}).get("dateTime"))
            if end and end < start + timedelta(days=1):
                out.setdefault((start, summary), (end, end.strftime("→%H:%M")))
            else:
                out.setdefault((start, summary), (None, "all day"))
        elif dt:
            out.setdefault((dt, summary), (dt, dt.strftime("%H:%M")))
        else:
            out.setdefault((start, summary), (None, "all day"))
    rows = sorted(out.items(), key=lambda kv: (kv[0][0], kv[1][1] != "all day"))
    return [(at, lbl, s) for (_, s), (at, lbl) in rows]


def visible_events(events, now):
    """The first MAX_EVENTS agenda rows as [(label, summary)].

    A timed event hides EVENT_GRACE_MIN after its shown time (an 08:00 event
    shows until 08:15). This runs at render time, not in the HA poll, so an
    event hides on time even between polls.
    """
    grace = timedelta(minutes=EVENT_GRACE_MIN)
    rows = [(lbl, s) for at, lbl, s in events if at is None or now <= at + grace]
    return rows[:MAX_EVENTS]


def fetch_events():
    """Today's events from every HA_CALENDAR_ENTITY (see parse_events), or None."""
    if not HA_CALENDAR_ENTITIES:
        return None
    now = datetime.now(TZINFO)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    params = {"start": start.isoformat(), "end": (start + timedelta(days=1)).isoformat()}
    calendars = [_ha("GET", f"/api/calendars/{c}", **params) for c in HA_CALENDAR_ENTITIES]
    calendars = [data for data in calendars if data is not None]
    if not calendars:  # every fetch failed; [] is a real "nothing today"
        return None
    return parse_events(calendars, start)


# --- HA data cache: a background thread keeps render() free of network I/O --
# render() runs on every GET /dash.png (~once a minute); doing the four HA
# fetches there would block the response on serial network calls. Instead poll
# them in the background and have the widgets read this snapshot (rebound
# atomically, so a reader always sees one consistent dict).
HA_DATA = {
    "weather": (None, None, None), "forecast": None, "hourly": None,
    "sun": (None, None), "events": None,
}


def ha_poll_loop():
    global HA_DATA
    while True:
        HA_DATA = {
            "weather": fetch_weather(),
            "forecast": fetch_forecast(),
            "hourly": fetch_hourly(),
            "sun": fetch_sun(),
            "events": fetch_events(),
        }
        time.sleep(DASH_POLL_MIN * 60)


# --- Claude usage: OAuth token refresh + usage fetch -----------------------
def read_usage(path=USAGE_FILE):
    try:
        with open(path) as f:
            d = json.load(f)
        stale = datetime.now(timezone.utc) - datetime.fromisoformat(d["updated"]) > timedelta(
            minutes=STALE_AFTER_MIN
        )
        return d, stale
    except Exception:
        return None, None


def _load_tokens():
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {"refresh_token": CLAUDE_REFRESH_TOKEN, "access_token": "", "expires_at": 0}


def _save_tokens(tok):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tok, f)
    os.replace(tmp, TOKEN_FILE)


def _refresh_access(tok):
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": CLAUDE_CLIENT_ID,
    }
    hdrs = {"User-Agent": CLAUDE_USER_AGENT}
    r = requests.post(CLAUDE_TOKEN_URL, json=payload, headers=hdrs, timeout=15)
    if r.status_code >= 400:  # some OAuth servers want form-encoding, not JSON
        r = requests.post(CLAUDE_TOKEN_URL, data=payload, headers=hdrs, timeout=15)
    r.raise_for_status()
    d = r.json()
    tok["access_token"] = d["access_token"]
    if d.get("refresh_token"):  # rotates — keep the new one or we lock ourselves out
        tok["refresh_token"] = d["refresh_token"]
    tok["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=int(d.get("expires_in", 3600)))
    ).timestamp()
    _save_tokens(tok)
    return tok


def _access_token():
    tok = _load_tokens()
    if not tok.get("refresh_token"):
        raise RuntimeError("no refresh token seeded (CLAUDE_REFRESH_TOKEN)")
    if not tok.get("access_token") or tok.get("expires_at", 0) < datetime.now(timezone.utc).timestamp() + 120:
        tok = _refresh_access(tok)
    return tok["access_token"]


def parse_claude_usage(data, now=None):
    """Normalize one Claude usage payload without network or credential access."""
    now = now or datetime.now(timezone.utc)

    def fmt(iso):
        if not iso:
            return ""
        try:
            return datetime.fromisoformat(iso).astimezone(TZINFO).strftime("%b %d, %H:%M")
        except Exception:
            return ""

    s, w = data.get("five_hour") or {}, data.get("seven_day") or {}
    extra = data.get("extra_usage") or {}

    def pct(value):
        return round(value["utilization"]) if value.get("utilization") is not None else None

    return {
        "session_pct": pct(s), "session_reset": fmt(s.get("resets_at")),
        "week_pct": pct(w), "week_reset": fmt(w.get("resets_at")),
        "extra_enabled": bool(extra.get("is_enabled")),
        "extra_pct": pct(extra),
        "extra_used": extra.get("used_credits"),
        "extra_limit": extra.get("monthly_limit"),
        "extra_currency": extra.get("currency") or "USD",
        "updated": now.isoformat(),
    }


def fetch_usage_once():
    def get(token):
        return requests.get(
            CLAUDE_USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": CLAUDE_USER_AGENT,
            },
            timeout=15,
        )

    r = get(_access_token())
    if r.status_code == 401:
        r = get(_refresh_access(_load_tokens())["access_token"])
    r.raise_for_status()
    return parse_claude_usage(r.json())


def usage_poll_loop():
    while True:
        try:
            rec = fetch_usage_once()
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(USAGE_FILE, "w") as f:
                json.dump(rec, f)
            print(f"[usage] session={rec['session_pct']}% week={rec['week_pct']}%", flush=True)
        except Exception as e:
            print(f"[usage] fetch failed: {e}", flush=True)
        time.sleep(CLAUDE_POLL_MIN * 60)


def _usage_poll(fetch_once, path, label, interval_min):
    """Generic poll loop: fetch a usage record and cache it to `path`."""
    while True:
        try:
            rec = fetch_once()
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(path, "w") as f:
                json.dump(rec, f)
            print(f"[{label}] ok", flush=True)
        except Exception as e:
            print(f"[{label}] fetch failed: {e}", flush=True)
        time.sleep(interval_min * 60)


def _reset_fmt(seconds=None, at=None, now=None):
    """Format a reset time (relative seconds, or absolute epoch) as local text."""
    try:
        if seconds is not None:
            dt = (now or datetime.now(timezone.utc)) + timedelta(seconds=int(seconds))
        elif at is not None:
            v = float(at)
            if v > 2_000_000_000_000:  # milliseconds, not seconds
                v /= 1000.0
            dt = datetime.fromtimestamp(v, timezone.utc)
        else:
            return ""
        return dt.astimezone(TZINFO).strftime("%b %d, %H:%M")
    except Exception:
        return ""


# --- Codex usage: OAuth token refresh + ChatGPT-subscription usage fetch ----
def _jwt_account_id(id_token):
    """Pull chatgpt_account_id out of the OAuth id_token JWT payload."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return (data.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
    except Exception:
        return None


def _codex_load_tokens():
    try:
        with open(CODEX_TOKEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {"refresh_token": CODEX_REFRESH_TOKEN, "access_token": "",
                "expires_at": 0, "account_id": CODEX_ACCOUNT_ID}


def _codex_save_tokens(tok):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = CODEX_TOKEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tok, f)
    os.replace(tmp, CODEX_TOKEN_FILE)


def _codex_refresh(tok):
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": CODEX_CLIENT_ID,
    }
    hdrs = {"User-Agent": CODEX_USER_AGENT}
    r = requests.post(CODEX_TOKEN_URL, json=payload, headers=hdrs, timeout=15)
    if r.status_code >= 400:  # some OAuth servers want form-encoding, not JSON
        r = requests.post(CODEX_TOKEN_URL, data=payload, headers=hdrs, timeout=15)
    r.raise_for_status()
    d = r.json()
    tok["access_token"] = d["access_token"]
    if d.get("refresh_token"):  # rotates — keep the new one or we lock ourselves out
        tok["refresh_token"] = d["refresh_token"]
    if d.get("id_token"):
        aid = _jwt_account_id(d["id_token"])
        if aid:
            tok["account_id"] = aid
    tok["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=int(d.get("expires_in", 3600)))
    ).timestamp()
    _codex_save_tokens(tok)
    return tok


def _codex_token():
    tok = _codex_load_tokens()
    if not tok.get("refresh_token"):
        raise RuntimeError("no refresh token seeded (CODEX_REFRESH_TOKEN)")
    if not tok.get("access_token") or tok.get("expires_at", 0) < datetime.now(timezone.utc).timestamp() + 120:
        tok = _codex_refresh(tok)
    return tok


def parse_codex_usage(data, now=None):
    """Normalize one Codex usage payload without network or credential access."""
    now = now or datetime.now(timezone.utc)
    rl = data.get("rate_limit") or {}
    prim, sec = rl.get("primary_window") or {}, rl.get("secondary_window") or {}

    def pct(x):
        return round(x["used_percent"]) if x.get("used_percent") is not None else None

    def reset(x):
        return _reset_fmt(
            seconds=x.get("reset_after_seconds"), at=x.get("reset_at"), now=now
        )

    credits = data.get("credits") or {}
    windows = []
    for window in (prim, sec):
        seconds = window.get("limit_window_seconds")
        label = "5h" if seconds == 18_000 else "7d" if seconds == 604_800 else None
        if label and pct(window) is not None:
            windows.append({"label": label, "pct": pct(window), "reset": reset(window)})

    return {
        "windows": windows,
        "credit_balance": credits.get("balance") if credits.get("has_credits") else None,
        "credit_unlimited": bool(credits.get("unlimited")),
        "plan": data.get("plan_type"),
        "updated": now.isoformat(),
    }


def fetch_codex_usage_once():
    def get(tok):
        hdrs = {"Authorization": f"Bearer {tok['access_token']}", "User-Agent": CODEX_USER_AGENT}
        if tok.get("account_id"):
            hdrs["ChatGPT-Account-Id"] = tok["account_id"]
        return requests.get(CODEX_USAGE_URL, headers=hdrs, timeout=15)

    tok = _codex_token()
    r = get(tok)
    if r.status_code == 401:
        r = get(_codex_refresh(_codex_load_tokens()))
    r.raise_for_status()
    return parse_codex_usage(r.json())


# --- opencode Go usage: scrape the dashboard HTML (no API exists) -----------
def _oc_window(name, text):
    """Pull (usagePercent, resetInSec) out of a named dashboard usage object."""
    m = re.search(name + r'["\']?\s*:\s*(?:\$R\[\d+\]\s*=\s*)?\{([^{}]*)\}', text, re.S)
    if not m:
        return None, None
    body = m.group(1)

    def num(field):
        mm = re.search(field + r'["\']?\s*:\s*"?(-?\d+(?:\.\d+)?)"?', body)
        return float(mm.group(1)) if mm else None

    return num("usagePercent"), num("resetInSec")


def parse_opencode_usage(text, now=None):
    """Normalize one OpenCode dashboard response without network or cookies."""
    text = html.unescape(text).replace('\\"', '"').replace("\\u0022", '"')
    now = now or datetime.now(timezone.utc)
    rec: dict = {"updated": now.isoformat()}
    found = False
    for field, key in (("rollingUsage", "fivehr"), ("weeklyUsage", "week"), ("monthlyUsage", "month")):
        up, rs = _oc_window(field, text)
        if up is None:
            continue
        found = True
        rec[f"{key}_pct"] = round(up)
        rec[f"{key}_reset"] = _reset_fmt(seconds=rs, now=now) if rs else ""
    if not found:
        raise RuntimeError("no usage windows found (cookie expired or dashboard markup changed)")
    return rec


def fetch_opencode_once():
    url = OPENCODE_GO_URL.format(ws=OPENCODE_WORKSPACE_ID)
    cookie = OPENCODE_AUTH_COOKIE if "auth=" in OPENCODE_AUTH_COOKIE else f"auth={OPENCODE_AUTH_COOKIE}"
    r = requests.get(
        url,
        headers={"Cookie": cookie, "User-Agent": OPENCODE_USER_AGENT,
                 "Accept": "text/html,application/xhtml+xml"},
        timeout=15,
    )
    r.raise_for_status()
    return parse_opencode_usage(r.text)


# --- OpenRouter usage: sum this month's spend across all API keys ----------
def parse_openrouter_usage(keys, now=None):
    """Normalize a list of OpenRouter key objects without network or credentials."""
    now = now or datetime.now(timezone.utc)
    utc = now.astimezone(timezone.utc)
    # usage_monthly resets at 00:00 UTC on the 1st.
    nxt = datetime(utc.year + utc.month // 12, utc.month % 12 + 1, 1, tzinfo=timezone.utc)
    return {
        "month_usd": round(sum(float(k.get("usage_monthly") or 0) for k in keys), 2),
        "month_reset": _reset_fmt(at=nxt.timestamp()),
        "keys": len(keys),
        "updated": now.isoformat(),
    }


def fetch_openrouter_once():
    keys, seen = [], set()
    while True:
        r = requests.get(
            OPENROUTER_KEYS_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_MANAGEMENT_KEY}"},
            params={"include_disabled": "true", "include_expired": "true", "offset": len(keys)},
            timeout=15,
        )
        r.raise_for_status()
        page = r.json().get("data")
        if not isinstance(page, list):
            raise RuntimeError("no key list in response")
        # Stop on an empty page, or on one with no new keys (offset ignored).
        new = [k for k in page if k.get("hash") not in seen]
        if not new:
            break
        seen.update(k.get("hash") for k in new)
        keys += new
    return parse_openrouter_usage(keys)


# --- drawing primitives ----------------------------------------------------
def draw_weather_icon(dr, cx, cy, r, cond):
    """Filled grayscale weather glyph centred at (cx, cy), scale r, by condition."""
    c = (cond or "").lower()
    B = 0
    lw = max(2, int(r * 0.12))

    def cloud(ox, oy, s):
        dr.ellipse((ox - 1.7 * s, oy - 0.1 * s, ox - 0.1 * s, oy + 1.1 * s), fill=B)
        dr.ellipse((ox - 0.7 * s, oy - 0.8 * s, ox + 0.9 * s, oy + 0.9 * s), fill=B)
        dr.ellipse((ox + 0.1 * s, oy - 0.2 * s, ox + 1.7 * s, oy + 1.1 * s), fill=B)
        dr.rectangle((ox - 1.5 * s, oy + 0.4 * s, ox + 1.5 * s, oy + 1.1 * s), fill=B)

    def sun(ox, oy, rad):
        for k in range(8):
            a = k * math.pi / 4
            dr.line(
                (ox + math.cos(a) * rad * 1.25, oy + math.sin(a) * rad * 1.25,
                 ox + math.cos(a) * rad * 1.75, oy + math.sin(a) * rad * 1.75),
                fill=B, width=lw,
            )
        dr.ellipse((ox - rad, oy - rad, ox + rad, oy + rad), fill=B)

    def streaks(ox, oy, s, dots):
        for i in range(3):
            x = ox - 0.6 * s + i * 0.6 * s
            if dots:
                dr.ellipse((x - 0.09 * s, oy, x + 0.09 * s, oy + 0.18 * s), fill=B)
            else:
                dr.line((x, oy, x - 0.18 * s, oy + 0.7 * s), fill=B, width=lw)

    if "night" in c and "cloud" not in c and "rain" not in c:
        dr.ellipse((cx - r, cy - r, cx + r, cy + r), fill=B)
        dr.ellipse((cx - r + 0.5 * r, cy - r - 0.1 * r, cx + r + 0.6 * r, cy + r - 0.1 * r), fill=255)
    elif "sun" in c or "clear" in c:
        sun(cx, cy, r * 0.6)
    elif "partly" in c:
        sun(cx - 0.3 * r, cy - 0.35 * r, r * 0.42)
        cloud(cx + 0.2 * r, cy + 0.15 * r, r * 0.5)
    elif "rain" in c or "pour" in c or "drizzle" in c or "hail" in c:
        cloud(cx, cy - 0.25 * r, r * 0.55)
        streaks(cx, cy + 0.9 * r, r, dots=False)
    elif "snow" in c:
        cloud(cx, cy - 0.25 * r, r * 0.55)
        streaks(cx, cy + 0.95 * r, r, dots=True)
    elif "fog" in c or "mist" in c or "haz" in c:
        for i in range(4):
            yy = cy - 0.55 * r + i * 0.4 * r
            dr.line((cx - r, yy, cx + r, yy), fill=B, width=lw)
    elif "light" in c or "thunder" in c:
        cloud(cx, cy - 0.25 * r, r * 0.55)
        dr.polygon(
            [(cx, cy + 0.7 * r), (cx - 0.25 * r, cy + 1.4 * r), (cx, cy + 1.3 * r),
             (cx - 0.1 * r, cy + 1.9 * r), (cx + 0.3 * r, cy + 1.0 * r), (cx + 0.05 * r, cy + 1.1 * r)],
            fill=B,
        )
    else:
        cloud(cx, cy, r * 0.6)


def _trunc(d, s, font, avail):
    if d.textlength(s, font=font) <= avail:
        return s
    while s and d.textlength(s + "…", font=font) > avail:
        s = s[:-1]
    return s + "…"


def _usage_block(d, box, title, rows, stale):
    """Draw a titled column of labeled % bars. rows = [(label, pct, reset)].

    A row may add a 4th item, the bar fill %, to fill the bar without printing
    a % (OpenRouter shows USD text over a budget bar).
    """
    x, y, w, h = box
    d.text((x, y), title + ("  (stale)" if stale else ""), font=_font(True, h * 0.12), fill=0)
    yy = y + h * 0.2
    if not rows:
        d.text((x, yy), "usage n/a", font=_font(False, h * 0.11), fill=0)
        return
    lf, sf = _font(False, h * 0.09), _font(False, h * 0.075)
    # Uniform pitch across columns (not h/len(rows)) so a 2-row provider's bars
    # line up with a 3-row one's instead of stretching to fill the column.
    rh = (h - (yy - y)) / USAGE_ROWS
    bh = min(rh * 0.34, h * 0.1)
    bx0, bx1 = x + w * 0.33, x + w * 0.97
    for i, (label, pct, reset, *bar) in enumerate(rows):
        ry = yy + i * rh
        d.text((x, ry), label, font=lf, fill=0)
        bar_pct = bar[0] if bar else pct
        if bar_pct is not None:
            fraction = max(0, min(1, bar_pct / 100))
            inner_x0, inner_x1 = bx0 + 3, bx1 - 3
            inner_width = inner_x1 - inner_x0
            fill_width = max(1, round(inner_width * fraction)) if fraction else 0
            if fill_width:
                d.rectangle(
                    (inner_x0, ry + 3, inner_x0 + fill_width - 1, ry + bh - 3),
                    fill=0,
                )
        d.rectangle((bx0, ry, bx1, ry + bh), outline=0, width=3)
        line = f"{pct}%" if pct is not None else ""
        if reset:
            line += ("  ·  " if line else "") + reset
        if not line:
            line = "—"
        d.text((bx0, ry + bh + h * 0.01), _trunc(d, line, sf, bx1 - bx0), font=sf, fill=0)


def parse_battery(q):
    """(pct, charging) from the device's ?battery=&charging= query, or None."""
    try:
        pct = int(q["battery"][0])
    except (KeyError, ValueError):
        return None
    if not 0 <= pct <= 100:
        return None
    return pct, q.get("charging", ["0"])[0] == "1"


# --- widgets: each draws inside box = (x, y, w, h) -------------------------
def w_clock(d, box):
    x, y, w, h = box
    now = datetime.now(TZINFO)
    d.text((x, y), now.strftime("%H:%M"), font=_font(True, h * 0.62), fill=0)
    d.text((x, y + h * 0.66), now.strftime("%a - %d/%m/%Y"), font=_font(False, h * 0.2), fill=0)


def w_weather(d, box, right=None):
    """Icon, temperature, condition. The temperature ends left of `right`
    (default: the box edge): its font shrinks for a long reading like 100° or
    -10°, so it never runs into the battery that shares its row."""
    x, y, w, h = box
    cond, temp, feels = HA_DATA["weather"]
    r = h * 0.32
    cx, cy = x + r * 1.5, y + h * 0.42
    draw_weather_icon(d, cx, cy, r, cond if temp is not None else "cloudy")
    tx = cx + r + 16
    if temp is not None:
        t = f"{round(temp)}°"
        size = h * 0.6
        # Keep the full-size baseline, so a smaller font stays on the same line.
        base = y + _font(True, size).getmetrics()[0]
        limit = (x + w if right is None else right) - tx
        while size > h * 0.3 and d.textlength(t, font=_font(True, size)) > limit:
            size -= 1
        d.text((tx, base), t, font=_font(True, size), fill=0, anchor="ls")
        sub = cond or ""
        if feels is not None:
            sub = (sub + f"  feels {round(feels)}°").strip()
        d.text((tx, y + h * 0.62), sub, font=_font(False, h * 0.16), fill=0)
    else:
        d.text((tx, y + h * 0.3), "weather n/a", font=_font(False, h * 0.18), fill=0)


def w_forecast(d, box):
    x, y, w, h = box
    days = HA_DATA["forecast"]
    if not days:
        return
    cw = w / len(days)
    for i, (label, cond, hi, lo) in enumerate(days):
        cx = x + cw * (i + 0.5)
        d.text((cx - cw * 0.4, y), label, font=_font(True, h * 0.16), fill=0)
        draw_weather_icon(d, cx, y + h * 0.42, h * 0.16, cond)
        t = f"{round(hi)}°" if hi is not None else "—"
        if lo is not None:
            t += f"/{round(lo)}"
        d.text((cx - cw * 0.4, y + h * 0.74), t, font=_font(False, h * 0.16), fill=0)


def w_agenda(d, box):
    x, y, w, h = box
    d.text((x, y), "TODAY", font=_font(True, h * 0.14), fill=0)
    yy = y + h * 0.2
    events = HA_DATA["events"]
    f = _font(False, h * 0.13)
    if events is None:
        d.text((x, yy), "no calendar", font=f, fill=0)
        return
    events = visible_events(events, datetime.now(TZINFO))
    if not events:
        d.text((x, yy), "nothing scheduled", font=f, fill=0)
        return
    tcol = w * 0.28
    for label, summary in events:
        d.text((x, yy), label, font=f, fill=0)
        d.text((x + tcol, yy), _trunc(d, summary, f, w - tcol), font=f, fill=0)
        yy += h * 0.17


def w_sun(d, box):
    x, y, w, h = box
    rise, sett = HA_DATA["sun"]
    f = _font(False, h * 0.62)
    d.text((x, y), "SUN", font=_font(True, h * 0.62), fill=0)
    d.text((x + w * 0.14, y), f"^ {rise.strftime('%H:%M')}" if rise else "^ --", font=f, fill=0)
    d.text((x + w * 0.36, y), f"v {sett.strftime('%H:%M')}" if sett else "v --", font=f, fill=0)


def w_rain(d, box):
    """Hour-by-hour rain probability: one bar per hour, % above, hour below."""
    x, y, w, h = box
    d.text((x, y), "RAIN CHANCE", font=_font(True, h * 0.14), fill=0)
    f = _font(False, h * 0.14)
    now = datetime.now(TZINFO).replace(minute=0, second=0, microsecond=0)
    hours = [(dt, pct) for dt, pct in HA_DATA["hourly"] or [] if dt >= now][:RAIN_HOURS]
    if not hours:
        d.text((x, y + h * 0.25), "rain n/a", font=f, fill=0)
        return
    # Fixed pitch (not w/len(hours)) so bars keep their width when HA returns
    # fewer hours than RAIN_HOURS.
    cw = w / RAIN_HOURS
    bw = cw * 0.6
    top, base = y + h * 0.40, y + h * 0.80
    # Shrink the value font until the widest label ("100%") fits its column.
    vf = f
    while vf.size > 8 and d.textlength("100%", font=vf) > cw * 0.92:
        vf = _font(False, vf.size - 1)
    for i, (dt, pct) in enumerate(hours):
        cx = x + cw * (i + 0.5)
        d.rectangle((cx - bw / 2, top, cx + bw / 2, base), outline=0, width=1)  # 100% track
        if pct:
            d.rectangle((cx - bw / 2, base - (base - top) * min(pct, 100) / 100, cx + bw / 2, base), fill=0)
        label = "—" if pct is None else f"{pct}%"
        d.text((cx, y + h * 0.22), label, font=vf, fill=0, anchor="ma")
        d.text((cx, base + h * 0.05), dt.strftime("%H"), font=f, fill=0, anchor="ma")
    d.line((x, base, x + cw * len(hours), base), fill=0, width=2)


def _battery_layout(d, box, pct):
    """w_battery geometry: (glyph left, glyph right, text right, text font, text left)."""
    x, y, w, h = box
    nub = h * 0.7 * 0.15
    bx1 = x + w - nub
    bx0 = bx1 - h * 0.7 * 1.9
    f = _font(False, h * 0.75)
    tx = bx0 - h * 0.3
    return bx0, bx1, tx, f, tx - d.textlength(f"{pct}%", font=f)


def battery_left(d, box, battery):
    """Left edge of what w_battery draws (the bolt or the text), or None."""
    if battery is None:
        return None
    pct, charging = battery
    h = box[3]
    left = _battery_layout(d, box, pct)[4]
    return left - h * 0.2 - h * 0.4 if charging else left


def w_battery(d, box, battery=None):
    """Kindle battery, right-aligned: "82%" and a small filled battery glyph.

    battery = (pct, charging) from the request (see parse_battery). Nothing is
    drawn when the device does not send it. A bolt shows while it charges.
    """
    if battery is None:
        return
    pct, charging = battery
    x, y, w, h = box
    cy = y + h / 2
    lw = max(2, round(h * 0.09))
    bh = h * 0.7
    nub = bh * 0.15
    bx0, bx1, tx, f, left = _battery_layout(d, box, pct)
    d.rectangle((bx0, cy - bh / 2, bx1, cy + bh / 2), outline=0, width=lw)
    d.rectangle((bx1, cy - bh * 0.22, bx1 + nub, cy + bh * 0.22), fill=0)
    gap = lw + 2
    fill = round((bx1 - bx0 - 2 * gap) * pct / 100)
    if fill:
        d.rectangle((bx0 + gap, cy - bh / 2 + gap, bx0 + gap + fill - 1, cy + bh / 2 - gap), fill=0)
    d.text((tx, cy), f"{pct}%", font=f, fill=0, anchor="rm")
    if charging:
        bw, bolt_h = h * 0.4, h * 0.7
        ox = left - h * 0.2 - bw
        oy = cy - bolt_h / 2
        pts = [(0.6, 0), (0, 0.55), (0.45, 0.55), (0.35, 1), (1, 0.4), (0.55, 0.4)]
        d.polygon([(ox + px * bw, oy + py * bolt_h) for px, py in pts], fill=0)


def _rows(usage, keys):
    """Build _usage_block rows [(label, pct, reset)] from a cached usage dict."""
    return [(label, usage.get(f"{k}_pct"), usage.get(f"{k}_reset", "")) for label, k in keys]


def w_usage(d, box):
    usage, stale = read_usage()
    rows = _rows(usage, [("5h", "session"), ("7d", "week")]) if usage else []
    _usage_block(d, box, "Claude", rows, stale)


def w_codex(d, box):
    usage, stale = read_usage(CODEX_USAGE_FILE)
    rows = [
        (window["label"], window.get("pct"), window.get("reset", ""))
        for window in (usage or {}).get("windows", [])
    ]
    if usage and usage.get("credit_unlimited"):
        rows.append(("Credits", None, "unlimited"))
    elif usage and usage.get("credit_balance") is not None:
        rows.append(("Credits", None, f"{usage['credit_balance']} left"))
    _usage_block(d, box, "Codex", rows, stale)


def w_opencode(d, box):
    usage, stale = read_usage(OPENCODE_USAGE_FILE)
    keys = [("5h", "fivehr"), ("Week", "week"), ("Month", "month")]
    _usage_block(d, box, "opencode", _rows(usage, keys) if usage else [], stale)


def w_openrouter(d, box):
    usage, stale = read_usage(OPENROUTER_USAGE_FILE)
    rows = []
    if usage:
        spend, budget = usage["month_usd"], OPENROUTER_MONTH_BUDGET
        text = f"${spend:.2f}" + (f" / ${budget:g}" if budget else "")
        if usage.get("month_reset"):
            text += f"  ·  {usage['month_reset']}"
        rows.append(("Month", None, text, round(100 * spend / budget) if budget else None))
    _usage_block(d, box, "OpenRouter", rows, stale)


WIDGET_FNS = {
    "clock": w_clock, "weather": w_weather, "agenda": w_agenda,
    "forecast": w_forecast, "sun": w_sun, "rain": w_rain,
    "usage": w_usage, "codex": w_codex, "opencode": w_opencode,
    "openrouter": w_openrouter, "battery": w_battery,
}
# Providers that share the bottom usage strip (split into columns, in order).
USAGE_NAMES = ("usage", "codex", "opencode", "openrouter")
# Row slots reserved per usage column = the most any provider shows (opencode =
# 3). Fixing this gives every column the same row pitch so bars align.
USAGE_ROWS = 3


def render(rotate=None, battery=None):
    # rotate: None → use DASH_ROTATE (the container setting). A ?rotate= query
    # can still override per-request, but the device never needs to.
    # battery: (pct, charging) the device sent with this request, or None.
    if rotate is None:
        rotate = DASH_ROTATE
    W, H = KINDLE_W, KINDLE_H
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    pad = max(16, W // 42)

    # --- layout grid: box per widget name; DASH_WIDGETS picks which to draw.
    # Widgets are isolated — one that raises is logged and skipped, never taking
    # down the whole frame.
    hdr_h = H * 0.20
    mid_y, mid_b = H * 0.26, H * 0.60
    mid = W * 0.52
    rcol = W - mid - pad
    midrow = mid_b - mid_y
    boxes = {
        "clock": (pad, pad, mid - pad, hdr_h),
        "weather": (mid, pad, rcol, hdr_h),
        "agenda": (pad, mid_y, mid - 2 * pad, midrow),
        "forecast": (mid, mid_y, rcol, midrow * 0.42),
        "sun": (mid, mid_y + midrow * 0.46, rcol, midrow * 0.1),
        "rain": (mid, mid_y + midrow * 0.6, rcol, midrow * 0.4),
        # small, in the top-right corner above the weather
        "battery": (W - pad - W * 0.2, pad, W * 0.2, H * 0.034),
    }
    # The bottom strip is the usage zone: split it into equal columns, one per
    # enabled usage provider (Claude / Codex / opencode), in DASH_WIDGETS order.
    strip_y = mid_b + 2 * pad
    usage_on = [n for n in DASH_WIDGETS if n in USAGE_NAMES]
    if usage_on:
        # Equal columns with even gutters, spanning pad‥W-pad exactly. Width is
        # proportional to the provider count (1 provider = full width, etc.).
        n = len(usage_on)
        cw = (W - 2 * pad - pad * (n - 1)) / n
        for i, name in enumerate(usage_on):
            boxes[name] = (pad + i * (cw + pad), strip_y, cw, H - strip_y - pad)
    # The battery shares the top row with the temperature: the temperature
    # stops half a pad left of it.
    temp_right = None
    if "battery" in DASH_WIDGETS:
        try:
            left = battery_left(d, boxes["battery"], battery)
        except Exception as e:  # same isolation as the widget loop below
            print(f"[widget] battery failed: {e}", flush=True)
            left = None
        if left is not None:
            temp_right = left - pad / 2
    extra = {"battery": (battery,), "weather": (temp_right,)}
    d.line((pad, H * 0.235, W - pad, H * 0.235), fill=0, width=2)
    d.line((pad, mid_b + pad, W - pad, mid_b + pad), fill=0, width=2)
    for name in DASH_WIDGETS:
        fn, box = WIDGET_FNS.get(name), boxes.get(name)
        if not (fn and box):
            continue
        try:
            fn(d, box, *extra.get(name, ()))
        except Exception as e:
            print(f"[widget] {name} failed: {e}", flush=True)

    if DASH_DARK:
        img = ImageOps.invert(img)  # light-on-black for e-ink dark mode
    if rotate in (90, 180, 270):
        img = img.rotate(rotate, expand=True)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path.startswith("/dash.png"):
            q = parse_qs(urlparse(self.path).query)
            try:  # optional per-request override; device omits it and gets DASH_ROTATE
                rotate = int(q["rotate"][0]) if "rotate" in q else None
            except ValueError:
                rotate = None
            try:
                png = render(rotate, parse_battery(q))
            except Exception as e:
                print(f"[render] failed: {e}", flush=True)
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)
            return
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    print(f"kindle-dash on :8080  ({KINDLE_W}x{KINDLE_H}, tz={TZ})", flush=True)
    if HA_BASE_URL or (RAIN_LAT and RAIN_LON):
        threading.Thread(target=ha_poll_loop, daemon=True).start()
    if CLAUDE_REFRESH_TOKEN or os.path.exists(TOKEN_FILE):
        threading.Thread(target=usage_poll_loop, daemon=True).start()
    if CODEX_REFRESH_TOKEN or os.path.exists(CODEX_TOKEN_FILE):
        threading.Thread(
            target=_usage_poll,
            args=(fetch_codex_usage_once, CODEX_USAGE_FILE, "codex", CODEX_POLL_MIN),
            daemon=True,
        ).start()
    if OPENCODE_WORKSPACE_ID and OPENCODE_AUTH_COOKIE:
        threading.Thread(
            target=_usage_poll,
            args=(fetch_opencode_once, OPENCODE_USAGE_FILE, "opencode", OPENCODE_POLL_MIN),
            daemon=True,
        ).start()
    if OPENROUTER_MANAGEMENT_KEY:
        threading.Thread(
            target=_usage_poll,
            args=(fetch_openrouter_once, OPENROUTER_USAGE_FILE, "openrouter", OPENROUTER_POLL_MIN),
            daemon=True,
        ).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
