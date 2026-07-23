# CLAUDE.md

Guidance for AI assistants (and humans) working in this repository. Read this
before making changes — this is a security-sensitive research codebase whose
whole point is that **model text is hostile input**. Several "obvious"
conveniences (dynamic commands, inherited env, trusting model output) are
deliberate anti-patterns here.

## What this project is

**AI Model Terrarium** is a deterministic, auditable world for studying cultural
evolution in populations of language-model agents. The central invariant, enforced
in code:

> Models live in the world; they never calculate the world.

An adapter (a model) may return **only** a strict game `Action`. Movement,
resources, weather, conflicts, hidden rules, damage, death and inheritance are all
resolved by a seeded Python world engine (NumPy PCG64 is the *only* random source).
Invalid, late or oversized model output becomes a recorded `noop` — never a crash,
never trusted state.

The current code targets the written-legacy **MVP**: one valley, six replaceable
agents, simultaneous ticks, deterministic mock agents for CI, hash-chained
append-only JSONL truth plus a rebuildable SQLite projection, and offline
measurement baselines. See `README.md` and `docs/architecture.md` for the full
scope and the "increment order" for future features.

## Repository layout

```
src/terrarium/          Sealed simulation core (the mechanics tree — treat as load-bearing)
  domain.py             Strict Action/state pydantic schemas — the trust boundary
  world.py              WorldEngine: locations, resources, health, hidden rules, sole RNG
  orchestrator.py       ExperimentRunner: concurrency, lifecycle, budgets, commit order
  storage.py            Crash-safe JSONL (authoritative) + rebuildable SQLite/WAL projection
  events.py             Canonical, hash-chained event envelopes
  inheritance.py        Immutable, tokenizer-bounded legacy texts with provenance
  measurement.py        Offline knowledge-survival + behavioral-adoption curves (read-only)
  prompting.py          AgentContext: model-facing interface rules, never world mechanics
  config.py             Strict, versioned RunConfig (rejects unknown/duplicate/unsafe YAML)
  manifest.py           Reproducibility / supply-chain manifest (pins executable digests, etc.)
  budget.py             Concurrency-safe call gate + fail-closed token accounting
  replay.py             Deterministic re-execution of committed world transitions
  viewer.py             Allowlisted static viewer export (never exposes raw model text)
  factory.py            Builds adapters from sealed config (no dynamic command expansion)
  cli.py                JSON-only `terrarium` CLI entry point
  runtime/
    base.py             AgentAdapter Protocol + AdapterResult (the adapter contract)
    mock.py             DeterministicMockAdapter (default; used by CI and checked configs)
    claude_code.py      Tool-free Claude Code headless adapter
    subprocess.py       Sandboxed subprocess adapter (bubblewrap policy, resource limits)

src/terrarium_gui/      Optional loopback FastAPI GUI backend (separate package)
gui/                    GUI project: its own pyproject.toml + uv.lock, hatch build hook
  frontend/             React 19 + Vite + TypeScript frontend (ECharts, vitest)
configs/                Experiment configs: mvp.yaml (mock, checked), pilot.yaml (lethal rehearsal)
docs/                   architecture.md, adapter-protocol.md, claude-code.md, gui.md
scripts/start-gui.sh    Convenience launcher: sync deps, build frontend, serve loopback GUI
tests/                  pytest suite; tests/gui/ covers the GUI package
pyproject.toml          Core package (ai-model-terrarium); uv.lock is a sealed-run replay input
```

### Two packages, two lockfiles — do not merge them

- **`ai-model-terrarium`** (root `pyproject.toml` / `uv.lock`): the sealed
  simulation. Runtime deps are intentionally minimal (numpy, pydantic, PyYAML,
  tiktoken). The root `uv.lock` is part of the **reproducibility record** — it is a
  sealed-run replay input. Do not add heavyweight deps here.
- **`ai-model-terrarium-gui`** (`gui/pyproject.toml` / `gui/uv.lock`): the GUI,
  which depends on the core as an editable path. It lives in a separate package and
  lockfile *specifically* so web dependencies (FastAPI, uvicorn, the frontend) never
  alter the sealed simulation lockfile. The GUI is a sibling; it must not modify the
  `src/terrarium/` mechanics tree.

## Development environment & commands

Requirements: **Python 3.12+** and **`uv`** (Node 22 + npm for the GUI frontend).

### Core simulation

```bash
UV_CACHE_DIR=/tmp/uv-cache uv sync --extra dev      # install core + dev deps
uv run terrarium run configs/mvp.yaml --output runs/mvp
uv run terrarium verify runs/mvp
uv run terrarium measure runs/mvp configs/mvp.yaml --output measurements/mvp
uv run ruff check .                                  # lint
uv run pytest                                        # tests
uv run pytest --cov=terrarium                        # tests with coverage
```

### GUI (optional)

```bash
UV_CACHE_DIR=/tmp/terrarium-uv-cache uv sync --project gui --group dev
cd gui/frontend && npm ci && npm run build && cd ../..
uv run --project gui terrarium-gui serve --runs-root runs --configs-dir configs
```

Frontend-only checks (run in `gui/frontend/`): `npm run test` (vitest),
`npm run typecheck` (`tsc -b`), `npm run build`. The `scripts/start-gui.sh` helper
syncs frozen deps, rebuilds the frontend when sources changed, and serves the
loopback GUI in one step. The GUI is loopback-only and prints a one-time URL; see
`docs/gui.md` for its security boundaries.

### Before you push — mirror CI

CI (`.github/workflows/ci.yml`, Python 3.12 / Node 22, uv 0.11.28) runs:

```bash
uv sync --frozen --extra dev
uv sync --project gui --frozen --group dev
( cd gui/frontend && npm ci && npm run test && npm run build )
uv run --project gui --frozen ruff check .
uv run --project gui --frozen pytest --cov=terrarium --cov=terrarium_gui --cov-report=term-missing
```

It then builds and inspects the GUI sdist/wheel (asserts the frontend static build
is packaged and `node_modules` is excluded). Action SHAs are pinned; keep them so.

## CLI reference (`terrarium`)

The CLI is **JSON-only**: every command prints one JSON object to stdout; errors
print a stable `{"status":"error","error":<category>,...}` to stderr (raw
exception text is never leaked). Entry point: `terrarium.cli:main`.

| Command   | Purpose |
|-----------|---------|
| `run`     | Start or resume an experiment (`--output RUN_DIR`, optional `--max-ticks`) |
| `verify`  | Verify JSONL hash chain and SQLite projection integrity |
| `rebuild` | Rebuild SQLite from committed JSONL without trusting the old projection |
| `measure` | Write offline knowledge-survival + behavior measurements (output must be outside the run dir) |
| `viewer`  | Export the allowlisted static viewer |
| `replay`  | Re-execute recorded world mechanics and confirm determinism |
| `schema`  | Emit public JSON schema for `config` / `action` / `manifest` (default `all`) |

GUI entry point: `terrarium-gui serve ...` (`terrarium_gui.cli:main`).

## Run data layout

```
run/
  manifest.json     pinned experiment + runtime metadata (sealed)
  events.jsonl      append-only, hash-chained SOURCE OF TRUTH
  state.sqlite3     rebuildable query/checkpoint projection
  raw/              optional private adapter cache, never served by the viewer
```

A tick is durable only after its `tick_commit` event is flushed and `fsync`-ed. An
incomplete final JSONL tail is ignored on recovery; corruption in *committed*
history is fatal. SQLite can be deleted and rebuilt from the log at any time.

## Conventions & code style

- **Ruff** is the linter/formatter of record: `target-version = py312`,
  `line-length = 100`, rule sets `E, F, I, UP, B, ASYNC, S, RUF` (security rules
  `S` are on; `S101` assert is the only ignore). Keep `uv run ruff check .` clean.
- **Strict typing everywhere.** Modules start with `from __future__ import
  annotations`. Domain/config models subclass a `StrictModel` (pydantic
  `extra="forbid"`, `strict=True`, `frozen=True`) and deep-freeze nested containers
  — checkpoints are hash-addressed and must not drift in place. Prefer frozen
  dataclasses with `slots=True` for internal value types.
- **The domain is the trust boundary.** `domain.Action` has no free-text or command
  field and rejects unknown fields, duplicate keys and NaN. Only the
  orchestrator/domain validation boundary may turn adapter `payload` into an
  `Action`. Adapters return `AdapterResult`, never domain objects.
- **JSON is canonical:** sorted keys, `allow_nan=False`, UTF-8. This is how events
  are hashed and how the CLI emits output — preserve it.
- Read-only audit commands (`verify`, `rebuild`, `replay`) import orchestration/
  adapter machinery **lazily** so they stay usable for incident recovery. Keep that
  separation when adding commands.
- `pytest` uses `asyncio_mode = auto` (no `@pytest.mark.asyncio` needed),
  `--strict-markers --strict-config`. Put new tests under `tests/`; GUI tests under
  `tests/gui/`.

## Safety & security invariants — do not weaken these

This repo treats every model/legacy string as an **intentional intergenerational
prompt injection**. Prompt instructions and structured output are *not* a security
boundary. Safe defaults are fail-closed:

- **No `shell=True`, no dynamic command expansion, no inherited host environment.**
  Adapter executables are resolved and pinned by sha256; a digest mismatch fails the
  run rather than executing drifted code (`factory.py`).
- **Bounded I/O.** UTF-8/JSON input and output are size-limited; duplicate keys, NaN
  and unknown action fields are rejected.
- **`usage=None` ≠ zero usage.** A real CLI that omits token accounting must leave
  usage `None` so the budget governor fails closed. Never fabricate a zero.
- **Agents get no writable access** to the event log, SQLite, or hidden rules.
- **Model text is escaped** in the viewer and sanitized before operator logs — never
  interpolated into SQL, filesystem paths, or terminal output raw.
- **`process` isolation is not a strong sandbox.** Real agentic CLIs must use the
  bubblewrap policy (or an equivalent container/microVM worker) with an egress proxy
  that holds provider credentials. Network inheritance and host-process execution
  require explicit unsafe acknowledgement in config; if the requested boundary is
  unavailable, the adapter refuses to start.
- **Hash-chain + state-hash are verified before resume.** Do not change historical
  event semantics without bumping a schema version.

See `SECURITY.md` and `docs/claude-code.md` for the full threat model and the
credential-broker boundary.

## Adding features safely

- **New adapter / model runtime:** implement the `AgentAdapter` Protocol in
  `runtime/base.py` (`act`, `write_legacy`, `retell`, `answer_survey`, `close`),
  return `AdapterResult` (use `.success(...)` / `.failure(...)`), add the new value
  to the `RuntimeConfig.adapter` literal in `config.py`, and wire construction into
  `factory.build_adapter_factory`. Never return domain objects; never trust the
  payload — the orchestrator validates it. Model switching is an experimental
  intervention: it must start a new manifest or be a versioned event.
- **New action / mechanic:** extend `ActionKind` + the variant validator in
  `domain.py` and the resolution loops in `world.py:step` — and add a schema version.
- **`claude-code` runtime auth:** the adapter holds no credentials; it shells out to
  the installed `claude` binary headless (no tools, empty cwd) and lets that binary
  own authentication. **Do not add `--bare`** — current Claude Code disables
  OAuth/keychain reads in bare mode. `config.py` forces this adapter to
  `sandbox.backend=process` + `network=inherit` + explicit unsafe acknowledgement;
  run it as a dedicated OS user behind an egress proxy. See `docs/claude-code.md`.
- **Determinism is sacred.** Nothing about adapter completion order, wall-clock time,
  latency or prose may enter the world transition. All randomness flows through the
  engine's checkpointed PCG64 state.
- **Two lockfiles rule** (above): keep GUI/web deps out of the core package.
- **Increment order:** per `docs/architecture.md`, extensions (oral channel, second
  valley, shocks, surveys, mixed populations, steganography) land one at a time, and
  a new module must not alter historical event semantics without a schema version.

## Key documentation

- `README.md` — project summary, quick start, security model, data layout
- `SECURITY.md` — threat model and reporting
- `docs/architecture.md` — invariants, the tick transaction, data ownership, reproducibility levels
- `docs/adapter-protocol.md` — the exact one-shot adapter JSON contract
- `docs/claude-code.md` — the Claude Code headless runtime and host-execution boundary
- `docs/gui.md` — GUI operation and security details
