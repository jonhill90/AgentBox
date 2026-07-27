# Development

## Run it

```bash
cp .env.example .env          # optional; defaults are sane
docker compose up -d --build
curl http://localhost:8054/health
```

Published on `127.0.0.1:8054` only. `AGENTBOX_BIND` overrides that, but read
`docs/architecture/security.md` first — Docker publishes to all interfaces by default and, on
Linux, bypasses `ufw` entirely.

## Test it

```bash
python3 -m venv .venv-test
.venv-test/bin/pip install pytest pytest-asyncio 'mcp>=1.2.0' httpx websockets
.venv-test/bin/python -m pytest tests/ -q
```

260 tests (257 pass, 3 skip by design), about 4 minutes. These are **integration tests against a real
container** — there are no mocked-browser unit tests, because the thing worth
proving is that a real Chromium page survives real tool calls.

If nothing is listening on `/health` the suite runs `docker compose up -d
--build` itself and tears it down after; if a container is already up it uses
it and leaves it running.

The suite is **auth-aware**: it reads the same `.env` Compose reads and
attaches the token if one is configured, so it exercises whatever you are
actually running. Three "unauthenticated by default" assertions skip when a
token is set — that is expected, not a failure.

Several tests start extra throwaway containers on ports 8055–8063 to exercise
toggles, auth, and the terminal in configurations the main container is not
in. They clean up after themselves; if a run is interrupted, `docker rm -f`
anything named `agentbox-*`.

| File | Proves |
|---|---|
| `test_integration.py` | The page persists across calls and across connections |
| `test_error_paths.py` | Browser failures return structured errors, never kill the page |
| `test_resilience.py` | No deadlock under load, no Chromium leaks, restart recovery |
| `test_ui_api.py` | The `/api/*` surface, Describe mode, vendored assets |
| `test_jumpbox_tools.py` | Filesystem containment, git's fixed subcommands, SSRF |
| `test_ssrf_redirects.py` | Redirect hops, offline and deterministic |
| `test_policy.py` | `PathPolicy` units; allowlists still empty and unwired |
| `test_auth.py` | Bearer auth on every surface, both ways |
| `test_hardening.py` | Rebinding guard, token file, rotation overlap |
| `test_terminal.py` | PTY auth, echo round-trip, non-root, no leaks |
| `test_feature_toggle.py` | Gated tools genuinely absent when off |

## Try the terminal

Off by default. It needs **both** a toggle and a token:

```bash
docker run -d --name ab-demo \
  -e AGENTBOX_ENABLE_TERMINAL=true -e AGENTBOX_AUTH_TOKEN=demo \
  -p 127.0.0.1:8059:8000 agentbox-agentbox
# then open http://localhost:8059/ui and enter: demo
```

## Debugging

```bash
docker compose logs -f
docker top agentbox -eo pid,comm       # the image has no `ps` inside
docker compose exec agentbox bash      # note: exec bypasses the entrypoint, so it lands as root
docker volume rm agentbox_agentbox-workspace   # reset the workspace
```

The startup log states every decision it made — which toggles are on, where
the auth token came from (with a fingerprint, never the value), and the
rebinding allowlist. Read it before guessing.

## Watch out for

**Green tests are not enough for UI work.** Several real bugs here were
invisible to a passing suite and only appeared when the page was opened: a
`resize` frame sent before the auth handshake, a CSS rule that defeated the
`[hidden]` attribute, a font subset that rendered blank glyphs. If you touch
`src/ui.html`, open it in a browser and look.

**Vacuous terminal tests.** The shell prompt contains `agentbox` and
`/workspace`. Asserting on those against terminal output can pass even when
the command never ran. Two tests once sent a literal `\n` instead of a
newline and passed anyway. Assert on a string that cannot appear unless the
command actually executed, and check your byte literals.

**Third-party flakiness.** A few `http_request` tests hit `httpbin.org`,
which flaps. They skip on 5xx rather than failing; the redirect behaviour
they cover is proved deterministically and offline in
`test_ssrf_redirects.py`.

**Machine load.** Running another Docker stack alongside can starve the
suite and produce spurious failures in the container-heavy tests. Check what
else is running before chasing a flake.

## Conventions

- Tools return a **JSON string** with a `success` boolean. Failures come back
  as `{"success": false, "error": ...}` rather than raising, so a bad
  selector never kills the page.
- Error text is capped (300 chars) so a Playwright call log cannot flood a
  client.
- New tools go through `_tool()` / `_api_route()`, never the FastMCP
  decorators directly, or they will not be auth-gated.
- Anything feature-gated is **defined inside** its toggle's conditional.
- Ports 8055–8063 are used by tests; pick something else for scratch work.

## Reference material

hill90-app (`~/source/repos/Personal/hill90-app`) is the upstream this was
ported from and is **read-only**. Useful when porting: its
`services/agentbox/app/` holds the browser loop, `ws_terminal.py`,
`policy.py`, and `filesystem.py`; `services/ui/src/app/chat/` holds the
`SessionPane.tsx` and `XTerminal.tsx` the viewer models.

Where this repo deliberately differs from it is recorded in the relevant
`SPEC.md` section — for example the terminal keeps the `vcs` prompt segment
that hill90-app dropped, because gitstatusd is pre-fetched here.
