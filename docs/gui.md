# Scientific GUI

The GUI is a loopback-only FastAPI server with a React/Vite client.  It lives in
`src/terrarium_gui/` and `gui/frontend/`; no GUI code is part of the sealed
`src/terrarium/` package tree.

## Install and run

```bash
UV_CACHE_DIR=/tmp/terrarium-uv-cache uv sync --project gui --group dev
cd gui/frontend
npm ci
npm run build
cd ../..
uv run --project gui terrarium-gui serve --runs-root runs --configs-dir configs
```

The command binds only `127.0.0.1` and prints a URL whose fragment contains a
random session token.  Fragments are not sent in HTTP requests.  The client
moves the token to `sessionStorage`, removes it from the URL, and uses it as a
Bearer credential.  Uvicorn access logging is disabled so SSE query credentials
cannot be recorded.

For frontend development, place an explicit token of at least 24 bytes in a
private file so the secret never appears in the process list, then use the Vite
proxy:

```bash
umask 077
python -c 'import secrets; print(secrets.token_urlsafe(32))' > /tmp/terrarium-gui-token
uv run --project gui terrarium-gui --port 8765 --dev-token-file /tmp/terrarium-gui-token
cd gui/frontend
npm run dev
```

Open `http://127.0.0.1:5173/#token=...` using the value from the private token
file.  The client intentionally rejects query-string tokens.

## Filesystem boundaries

GUI Python dependencies use `gui/uv.lock`, not the root lock.  This separation
is a replay invariant: sealed manifests hash the root `uv.lock`, so adding web
dependencies there would invalidate every run created before the GUI existed.

- `runs-root/<run>/` is written only by `terrarium.cli`.  The GUI never opens an
  `EventStore` or creates the writer lock.  It copies the projection database
  and optional WAL through no-follow read descriptors into a cached GUI-owned
  temporary snapshot, then opens that snapshot with URI `mode=ro` plus
  `query_only`.  SQLite therefore cannot create or update `-shm` state in the
  sealed run directory.
- Config snapshots, process stdout/stderr, audit exports, and GUI history are
  stored beneath `artifacts-root` (default `.terrarium-gui-artifacts/`).
- Configuration CRUD is limited to single token names beneath `configs-dir`.
  Reads and atomic replacements reject symlinks and oversized files.
- Offline `measure` and `viewer` output is always placed in the GUI artifact
  tree, never in a sealed run directory.

Run status is inferred from the existing flock, manifest, and latest read-only
checkpoint.  A managed stop sends `SIGINT`, then escalates to `SIGTERM` and
`SIGKILL` only after bounded grace periods.  Resume always uses the preserved
configuration snapshot and the sealed CLI checks its digest against the
checkpoint.

## Live data contract

All API routes are under `/api/v1` and require the session Bearer token.  The
native `EventSource` exception is `GET /runs/{run}/stream`, where
`access_token` is accepted only on that exact route and scrubbed from the ASGI
query string before request handling.

Important endpoints:

- `/schema/config` and `/configs` provide schema-driven editing and strict YAML
  validation;
- `/runs`, `/runs/{run}/stop`, and `/runs/{run}/resume` manage CLI processes;
- `/runs/{run}/events` provides bounded REST backfill (`tail=true` selects the
  newest page for initial load) and
  `/runs/{run}/stream` provides SSE with `id: seq`, `Last-Event-ID`, and gap
  frames for slow subscribers;
- `/agents`, `/lineage`, and `/legacies` are paginated and report
  `offset/limit/total/has_more`; `/ticks` and token series use bounded recent
  windows, while `/world` reads a selected checkpoint;
- budget and token telemetry comes from the committed read-only SQLite
  projection; behavior and knowledge-survival curves come from a verified
  `terrarium measure` CLI artifact cached by committed run identity;
- `/tools/verify|rebuild|replay|measure|viewer-export` run asynchronous audit
  subprocesses and return `409` while a writer is active.

Indexed event reads are used only when the SQLite `(seq, hash)` head matches the
authoritative JSONL head.  If projection lags a durable fsync, backfill falls
back to committed JSONL framing, so the live subscription cannot skip the gap.
The tailer retains bytes only through the last `tick_commit`.  A physical or
fully written uncommitted tail is not emitted and is re-read after writer
recovery.  Per-client queues are bounded; overflow produces a gap frame and the
browser fills it through the REST event endpoint.

Automatic `measure` results are cached against the run's committed `(seq,
hash)` identity.  A terminal failure is also cached and displayed with an
explicit retry action, preventing a broken measurement command from spawning an
unbounded retry loop.

## Display safety

Event payloads pass through a closed allowlist and
`terrarium.storage.sanitize_control_text`.  `raw_text`, prompts, raw model
responses, RNG state, and opaque checkpoint data are never exposed.  React
renders hostile legacy text as ordinary text children; the frontend contains no
`dangerouslySetInnerHTML`.  The server applies a deny-by-default CSP, rejects
non-loopback Host headers, and adds no-sniff, no-referrer, frame, and permissions
headers.

## Verification

```bash
uv run --project gui ruff check .
uv run --project gui pytest
cd gui/frontend
npm run test
npm run typecheck
npm run build
```

Backend tests cover authentication, DNS-rebinding Host rejection, symlink-safe
config CRUD, committed transaction framing, stale-tail recovery, bounded SSE
gaps, read-only SQLite access, subprocess stop/resume, audit lock conflicts, and
post-resume `terrarium verify`.
