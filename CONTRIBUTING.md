# Contributing

PRs and issues welcome. It's a small project — keep changes focused and the dashboard's "fail soft, never a broken frame" contract intact.

## Dev shell (devenv + direnv)

The repo ships a [devenv](https://devenv.sh) shell (Python + Pillow/requests, `ruff`, `docker-compose`, fonts wired in).

```bash
# one-time: install devenv + direnv, then in the repo:
direnv allow          # or: devenv shell
```

Scripts available in the shell:

| Script | What |
|---|---|
| `preview` | render one frame to `./preview.png` (uses your local env; great for layout work) |
| `serve` | run the renderer on `:8080` |
| `fmt` | `ruff format` the renderer |
| `lint` | `ruff check` the renderer |

Iterate on layout/widgets with `preview` — no container, no Kindle needed. Set `DASH_LAT`/`DASH_LON` / `HA_*` / `CLAUDE_*` in your shell to exercise live sources, or monkeypatch the `fetch_*` functions in a scratch script.

## Conventions

- **Widgets**: optional (no config → hidden), fail soft (return `None`, never raise out of a widget). See [WIDGETS.md](WIDGETS.md).
- **Style**: pure black on white; size fonts off the box height; `ruff` clean.
- **Device scripts** (`kindle/`): POSIX `sh` only — the Kindle runs **busybox ash**. No bashisms (`10#`, `[[ ]]`, arrays). Check with `sh -n`.
- **Commits**: Conventional Commits (`feat`, `fix`, `docs`, …). No AI co-author trailers.
- **Secrets**: never commit `.env` or tokens.

## Release labels

Every merged PR is evaluated for a release from the protected merge commit.
Use at most one release label:

- `release:major`, `release:minor`, or `release:patch` selects that SemVer bump.
- `release:none` skips publication.
- No release label defaults to a minor bump.
- Conflicting release labels fail closed without publishing.

CI must pass before merge. The release workflow publishes the amd64 image by
digest, verifies its keyless signature, SBOM, and provenance, then creates the
Git tag. A tag is therefore evidence of a verified artifact, not the trigger
for building one.

## Scope

Core stays small and dependency-light (Pillow + requests). New data sources belong in widgets, not the core. Big layout ideas (portrait mode, multi-screen rotation) are welcome — open an issue first so we agree on the shape.
