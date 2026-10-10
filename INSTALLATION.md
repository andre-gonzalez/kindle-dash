# Installing kindle-dash

Two halves: the **renderer** (a container on any always-on machine the Kindle can reach over your LAN) and the **Kindle** (a jailbroken device that fetches the image). Do the renderer first — you can point a browser at it before the Kindle is ready.

## TL;DR

```bash
git clone https://github.com/ErikBPF/kindle-dash && cd kindle-dash
cp .env.example renderer/.env && $EDITOR renderer/.env   # set KINDLE_W/H, TZ, sources
cd renderer && docker compose up -d --build
curl -o test.png http://localhost:8810/dash.png          # should be a PNG
```

Then jailbreak the Kindle, drop `dashboard.sh` + the KUAL extension on it, and hit **Start**.

---

## 1. Renderer (container)

Requires Docker + compose.

```bash
cp .env.example renderer/.env
$EDITOR renderer/.env        # at minimum set KINDLE_W, KINDLE_H, TZ
cd renderer && docker compose up -d --build
```

It serves `http://<host>:8810/dash.png`. Open that in a browser — you should see the dashboard (panels with no configured source show a placeholder). See [CONFIGURATION.md](CONFIGURATION.md) for every variable and how to wire weather/agenda/usage.

> **The Kindle can't do HTTPS.** Its TLS stack is too old. Serve the renderer over **plain HTTP**. On a trusted LAN the published port is fine. To use a hostname / reverse proxy, keep a plain-HTTP `:80` vhost for the device — example in [`deploy/nginx-kindle-dash.conf.example`](deploy/nginx-kindle-dash.conf.example). (A browser will auto-upgrade to https; that's fine, the device keeps using http.)

## 2. Kindle (device)

### Prerequisites

1. **Jailbreak** — the exploit depends on your firmware; follow the current MobileRead thread for your model. You want a way to run shell commands (USBNetwork, or **KUAL + kterm**).
2. **A painter** — `dashboard.sh` uses the stock **`eips`** that ships on every jailbroken Kindle, so **nothing to install**. (It prefers `fbink` if present for nicer refreshes — optional: drop NiLuJe's binary at `/mnt/us/fbink` or set `FBINK=/path`.)
3. **`curl`** — present on most jailbroken Kindles.
4. Kindle and renderer on the **same LAN** — or on the same **Tailscale tailnet** (see [Away from home](#3-away-from-home-tailscale)).

### Confirm the screen size

The renderer must be told your exact resolution (it's set in `.env`, landscape — width ≥ height — and the device rotates). On the Kindle:

```sh
cat /sys/class/graphics/fb0/virtual_size    # e.g. 758,1024 on a Paperwhite 2
```

Paperwhite 2 = portrait `758×1024` → set `KINDLE_W=1024`, `KINDLE_H=758`. PW3+/Oasis = `1072×1448` → `1448`/`1072`.

### Copy the files

Mount the Kindle as a USB drive (its root is `/mnt/us`) and copy:

- `kindle/dashboard.sh` → `/mnt/us/documents/dashboard.sh`
- `kindle/extensions/kindledash/` → `/mnt/us/extensions/kindledash/`

Edit the URL in `dashboard.sh` (or export `DASH_URL`) to where the renderer is reachable **from the Kindle**, e.g. `http://192.168.1.10:8810/dash.png?rotate=90`.

### Run it

Eject/disconnect USB first (USB-drive mode disables Wi-Fi and locks the filesystem). Then:

- **KUAL**: open KUAL → **Kindle Dashboard** → **Start**. (Exit and reopen KUAL once after copying so it scans the new extension.)
- **kterm**: `sh /mnt/us/extensions/kindledash/start.sh`

The clock updates every minute; **Stop** closes it cleanly (restores the screensaver). See [USAGE.md](USAGE.md).

#### `rotate`

The image is landscape; `?rotate=90` spins it to fill the Kindle's portrait framebuffer when you mount the device **sideways**. If it lands upside down, use `?rotate=270`. For a desktop browser, drop `?rotate=`.

## 3. Away from home (Tailscale)

Optional. On your LAN the steps above are all you need. To keep the dashboard working when the Kindle leaves home (a trip, the office), fetch the image over a [Tailscale](https://tailscale.com) tailnet instead. The tailnet is WireGuard-encrypted end to end, so the Kindle's plain HTTP is fine on it — no TLS needed on the device.

You need:

- The renderer host on your tailnet (e.g. a node named `servarr`). Docker's default `0.0.0.0` bind already makes `:8810` reachable on its tailnet IP.
- The [KOReader Tailscale plugin](https://github.com/victoria-riley-barnett/koreader-tailscale) on the Kindle. Install it and log the Kindle in to your tailnet once from KOReader.

The plugin's `tailscaled` serves a local HTTP proxy on `127.0.0.1:1056` — in TUN **and** userspace mode — that resolves MagicDNS names. `dashboard.sh` sends its fetch through it. Set these two lines in `dashboard.sh`:

```sh
URL="${DASH_URL:-http://servarr:8810/dash.png}"
DASH_PROXY="${DASH_PROXY:-http://127.0.0.1:1056}"
```

Use the same URL at home and away: at home Tailscale connects directly over the LAN, so there is no extra hop.

KOReader starts `tailscaled` when it opens, but after a reboot straight into KUAL nothing has. So when `DASH_PROXY` is set and `tailscaled` isn't running, `dashboard.sh` starts it from the plugin's binaries (`TS_BIN`, default `/mnt/us/koreader/plugins/tailscale.koplugin/bin`). It runs the plugin's `tailscaled` with the same flags and state directory as the plugin, so the Kindle keeps its node identity, and the daemon resumes the last connected/disconnected state (no `tailscale up`). If you disconnected Tailscale in KOReader, connect it there again. **Stop** leaves `tailscaled` running — KOReader shares it, and it idles frozen while the device sleeps.

Lock it down with a Tailscale ACL so the Kindle can reach only the renderer — see [SECURITY.md](SECURITY.md#network-exposure).
