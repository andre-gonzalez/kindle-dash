#!/bin/sh
# dashboard.sh — run ON a jailbroken Kindle (tested on Paperwhite 2; works on
# most). Fetches the rendered PNG from the renderer and paints it on the e-ink
# screen, refreshing on a timer. A periodic full (flashing) refresh de-ghosts.
#
# Painter: prefers fbink (nicer partial refresh) but falls back to the stock
# `eips` that ships on every jailbroken Kindle — so no install is required.
# Requires curl (present on most jailbroken Kindles).
#
# SET THIS URL to wherever the renderer is reachable from the Kindle. The
# Kindle's ancient TLS can't do modern HTTPS, so serve plain HTTP (see
# INSTALLATION.md for a reverse-proxy example). All visual options — rotation,
# dark mode, which widgets — are set on the RENDERER (container env), so the
# device just opens the bare link.
URL="${DASH_URL:-http://CHANGE-ME.lan/dash.png}"
# Optional: fetch over your Tailscale tailnet, so the dashboard also works away
# from home. Needs the KOReader tailscale plugin, whose tailscaled always serves
# an HTTP proxy on 127.0.0.1:1056 (TUN or userspace mode). The proxy resolves
# MagicDNS names, e.g. DASH_URL=http://servarr:8810/dash.png with
# DASH_PROXY=http://127.0.0.1:1056. Empty = plain LAN fetch, no Tailscale.
DASH_PROXY="${DASH_PROXY:-}"
# The plugin's binaries. KOReader starts tailscaled on open; after a reboot
# straight into KUAL nothing has, so we start it from here (see ensure_tailscale).
TS_BIN="${TS_BIN:-/mnt/us/koreader/plugins/tailscale.koplugin/bin}"
IMG="/tmp/dash.png"
LOG="${LOG:-/tmp/kindle-dash.log}"
FULL_EVERY=30      # full flashing refresh every N cycles (here: 30 min) to de-ghost
# 1 = stop the stock Kindle UI so its idle timer can't suspend/lock the device
# (the real never-sleep fix for a wall dashboard). Reversible: cleanup restarts
# it, or reboot. Leave 0 if you still want the normal UI available.
STOP_FRAMEWORK="${STOP_FRAMEWORK:-0}"
# Refresh once per minute, aligned to the top of the minute (see loop below) so
# the clock flips with the wall clock instead of drifting up to ~2 min behind.

# Send all our stdout/stderr to a logfile so curl/shell chatter never paints
# onto the e-ink console (eips draws the image straight to the panel, so the
# dashboard itself is unaffected). Tail it with: cat $LOG
exec >>"$LOG" 2>&1

# --- pick a painter -------------------------------------------------------
# fbink if available (override with FBINK=/path), else the built-in eips.
FBINK="${FBINK:-fbink}"
if ! command -v "$FBINK" >/dev/null 2>&1 && [ ! -x "$FBINK" ]; then
  # Not /mnt/us/koreader/fbink: KOReader bundles a build without image support.
  for c in /mnt/us/fbink /mnt/us/bin/fbink \
           /mnt/us/extensions/fbink/bin/fbink /usr/local/bin/fbink; do
    [ -x "$c" ] && FBINK="$c" && break
  done
fi
if command -v "$FBINK" >/dev/null 2>&1 || [ -x "$FBINK" ]; then
  PAINT=fbink
elif command -v eips >/dev/null 2>&1; then
  PAINT=eips
else
  echo "no painter found (need fbink or the stock eips)" >&2
  exit 1
fi

paint() {  # $1 = 1 for a full flashing refresh, 0 for a fast partial one
  if [ "$PAINT" = fbink ]; then
    if [ "$1" = 1 ]; then "$FBINK" -c -f -g file="$IMG"
    else "$FBINK" -g file="$IMG"; fi && return
    # Some fbink builds are compiled without image support ("Image support is
    # disabled in this FBInk build!") — fall back to eips for good.
    echo "fbink at $FBINK can't draw images; falling back to eips"
    PAINT=eips
  fi
  [ "$1" = 1 ] && eips -c          # clear (de-ghost) on the full cycle
  eips -g "$IMG"
}

# --- Tailscale (only when DASH_PROXY is set) -------------------------------
# Start tailscaled the way the KOReader plugin does, if nothing has yet. We run
# the binary directly rather than the plugin's start script, whose interface
# differs between plugin versions. Same state dir as the plugin, so the Kindle
# keeps its node identity: bin/ if it takes chmod, else tmpfs seeded from bin/.
# No `tailscale up`: the daemon resumes its saved up/down state by itself.
# Userspace mode, as the plugin uses — we only need the proxy.
# Left running on Stop: KOReader shares it, and it idles frozen while asleep.
ensure_tailscale() {
  [ -n "$DASH_PROXY" ] || return 0
  pgrep tailscaled >/dev/null 2>&1 && return 0
  if [ ! -x "$TS_BIN/tailscaled" ]; then
    echo "DASH_PROXY is set but no tailscaled in $TS_BIN; is the plugin installed?"
    return 0
  fi
  statedir="$TS_BIN"
  if ! { touch "$TS_BIN/.chmod_test" && chmod 0600 "$TS_BIN/.chmod_test"; } 2>/dev/null; then
    statedir=/tmp/tailscale
    mkdir -p "$statedir"
    for f in tailscaled.state tailscaled.log.conf; do
      # don't clobber a newer tmpfs copy from earlier this boot
      [ -f "$TS_BIN/$f" ] && [ ! -f "$statedir/$f" ] && cp -f "$TS_BIN/$f" "$statedir/$f"
    done
  fi
  rm -f "$TS_BIN/.chmod_test" 2>/dev/null
  # the proxy binds 127.0.0.1; some firmwares leave loopback down
  ifconfig lo 2>/dev/null | grep -q '127\.0\.0\.1' ||
    ifconfig lo 127.0.0.1 netmask 255.0.0.0 up 2>/dev/null
  # KOReader's CA bundle beats the firmware's ancient one for the control plane
  ca=/mnt/us/koreader/data/ca-bundle.crt
  echo "starting tailscaled from $TS_BIN (state in $statedir)"
  (
    cd "$TS_BIN" || exit 1
    [ -r "$ca" ] && SSL_CERT_FILE="$ca" && export SSL_CERT_FILE
    XDG_CACHE_HOME="$statedir" ./tailscaled --statedir="$statedir/" \
      --tun=userspace-networking \
      --socks5-server=127.0.0.1:1055 \
      --outbound-http-proxy-listen=127.0.0.1:1056 \
      >tailscaled.log 2>&1 &
  )
  sleep 5  # let the proxy come up so this cycle's fetch has a chance
}

fetch() {
  if [ -n "$DASH_PROXY" ]; then
    curl -s -m 30 -x "$DASH_PROXY" -o "$IMG" "$URL"
  else
    curl -s -m 30 -o "$IMG" "$URL"
  fi
}

# Clean shutdown: on stop (KUAL Stop / pkill -TERM), restore the screensaver and
# clear the screen so the Kindle returns to normal without a reboot.
cleanup() {
  lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null
  [ "$STOP_FRAMEWORK" = 1 ] && { start lab126_gui 2>/dev/null || /etc/init.d/framework start 2>/dev/null; }
  command -v eips >/dev/null 2>&1 && eips -c 2>/dev/null
  exit 0
}
trap cleanup TERM INT

# Keep the screen from blanking while the dashboard is up.
lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null
# Kiosk mode: stop the stock UI so it can't idle-suspend/lock the device.
# (Command varies by firmware; try both. Reverse in cleanup.)
if [ "$STOP_FRAMEWORK" = 1 ]; then
  stop lab126_gui 2>/dev/null || /etc/init.d/framework stop 2>/dev/null
fi

i=0
while true; do
  # cheap when it's already up; restarts it if it died (or after a reboot)
  ensure_tailscale
  if fetch; then
    if [ $((i % FULL_EVERY)) -eq 0 ]; then paint 1; else paint 0; fi
  fi
  # re-assert each cycle — the framework can reset it after a while
  lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null
  # else: keep the last frame up; retry next cycle (Wi-Fi blip, renderer reboot)
  i=$((i + 1))
  # Sleep until the top of the next minute so the clock stays in sync. Strip a
  # leading zero first — busybox ash reads $((08)) as bad octal and aborts, and
  # the bash-only 10# base prefix isn't available here.
  secs=$(date +%S)
  secs=${secs#0}
  sleep $((60 - secs))
done
