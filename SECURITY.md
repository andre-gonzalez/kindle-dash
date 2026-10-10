# Security

## The Claude usage panel uses an undocumented API

The Session/Week/Sonnet panel calls `https://api.anthropic.com/api/oauth/usage` with an OAuth bearer token, and refreshes that token against `https://api.anthropic.com/v1/oauth/token`. **Neither endpoint is a documented, supported API.** They can change or vanish without notice, and using them may be subject to Anthropic's terms. This panel is **opt-in** — leave `CLAUDE_REFRESH_TOKEN` blank and none of it runs.

If the endpoints move, override `CLAUDE_TOKEN_URL` / `CLAUDE_USAGE_URL` (no rebuild needed).

## Give the dashboard its OWN login — don't share your workstation's

Claude refresh tokens **rotate**: each refresh invalidates the previous token and issues a new one. If the renderer and your laptop's Claude Code share one token lineage, whichever refreshes second gets rejected — and you'll find your laptop **logged out**.

So mint a dedicated session into a separate config dir (your `~/.claude` stays untouched):

```bash
mkdir -p ~/dash-claude-auth
env CLAUDE_CONFIG_DIR=$HOME/dash-claude-auth claude   # then /login, /exit
jq -r .claudeAiOauth.refreshToken ~/dash-claude-auth/.credentials.json
```

Put that refresh token in `CLAUDE_REFRESH_TOKEN`. The renderer then owns that lineage end to end and persists rotations to `/data/claude_tokens.json`.

## Codex (ChatGPT subscription) — same OAuth caveats

The Codex panel works like Claude's: it calls an **undocumented** ChatGPT endpoint (`https://chatgpt.com/backend-api/wham/usage`) and refreshes its token against `https://auth.openai.com/oauth/token`. Same rules — opt-in (`CODEX_REFRESH_TOKEN` blank = off), and give it its **own** `codex login` so token rotation can't log out your workstation's Codex CLI:

```bash
mkdir -p ~/dash-codex-auth
env CODEX_HOME=~/dash-codex-auth codex login          # then exit
jq -r .tokens.refresh_token ~/dash-codex-auth/auth.json
```

Put that in `CODEX_REFRESH_TOKEN`; rotations persist to `/data/codex_tokens.json`. The `ChatGPT-Account-Id` header is auto-derived from the id_token JWT — only set `CODEX_ACCOUNT_ID` if the usage call 401s.

## opencode Go — no API, so it scrapes (and the cookie expires)

opencode exposes **no usage API** ([upstream request #16017](https://github.com/anomalyco/opencode/issues/16017)). The panel fetches the Go dashboard HTML, authenticated by your browser's `auth` **session cookie**, and parses the usage out of it. Consequences:

- The cookie is **HttpOnly and short-lived** (days), with **no refresh path** — when it expires the panel goes `(stale)`. Re-grab and re-seed.
- `tools/opencode-capture.py` reads the cookie straight from your browser's encrypted cookie store (Brave/Chromium-family, via the OS keyring) and pulls the workspace id from history — no DevTools. Re-run it whenever the panel goes stale.
- It's markup-dependent: if opencode changes the dashboard, the panel fails soft to `usage n/a`, never a broken frame.

Set `OPENCODE_WORKSPACE_ID` + `OPENCODE_AUTH_COOKIE` (both opt-in; blank = panel off).

## Token handling

- The refresh token is a credential. Keep `renderer/.env` out of git (it's in `.gitignore`) and off shared storage; use your platform's secret mechanism if you have one.
- Tokens persist to the `/data` volume at mode of the container user. Treat that volume as sensitive.
- The usage limits are **account-wide**, so the renderer reports the same numbers regardless of which machine asks.

## Network exposure

The renderer serves **plain HTTP** with **no authentication** — because the Kindle can't do TLS. Keep it on a trusted LAN. The reverse-proxy example restricts the vhost to private ranges (`allow`/`deny`); adjust to your network. Don't expose `/dash.png` to the internet — it may reveal your calendar, location (via weather), and usage.

To use the Kindle away from home, reach the renderer over a **Tailscale tailnet** instead of opening a port (see [INSTALLATION.md](INSTALLATION.md#3-away-from-home-tailscale)). Tailnet traffic is WireGuard-encrypted, so plain HTTP is fine there. Add an ACL so the Kindle can reach only the renderer, e.g. with the Kindle tagged `tag:kindle`:

```jsonc
"hosts": { "kindle-dash": "100.x.y.z" },   // the renderer's tailnet IP
"acls": [
  { "action": "accept", "src": ["tag:kindle"], "dst": ["kindle-dash:8810"] }
]
```

If the Kindle is lost, remove it from the tailnet in the Tailscale admin console.

## Published image verification

Releases are built from protected `main` merge commits. The workflow publishes
an immutable GHCR digest with BuildKit SBOM and provenance, signs that digest
keylessly, and verifies the exact repository/workflow identity before creating
the Git tag. Consumers should pin the digest, not a mutable version tag.

## Reporting

Found something? Open an issue (or a private report if it's sensitive). This is a hobby project with no warranty (see [LICENSE](LICENSE)); fixes are best-effort.
