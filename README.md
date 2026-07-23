# AI Model Terrarium

AI Model Terrarium is a deterministic, persistent world for studying cultural
evolution in populations of language-model agents.  Models live in the world;
they never calculate the world.

The central invariant is enforced in code: an adapter may return only a strict
game action.  Movement, resources, weather, conflicts, hidden rules, damage,
death and inheritance selection are resolved by a seeded Python world engine.
Invalid, late or oversized model output becomes a recorded no-op.

## MVP implemented here

- one valley and six replaceable agents;
- simultaneous ticks with PCG64 as the only random source;
- deterministic poison, probabilistic collapse and a harmless engineered-correlation
  decoy cue; generation zero deterministically schedules 1--2 ordinary old-age
  deaths, leaving a nearby live witness, to seed the configured false hypothesis;
- checked MVP deaths limited to old age and poison; starvation and collapse are
  nonterminal pressures (the generic engine can opt into their lethality);
- immutable written legacies with tokenizer-level limits and provenance;
- hash-chained append-only JSONL plus a rebuildable SQLite/WAL projection;
- checkpoint/resume, deterministic replay and a concurrency-safe model-call gate;
- fail-closed accounting of adapter-reported token usage;
- deterministic mock agents for safe end-to-end and CI runs;
- a tool-free Claude Code headless runtime with checkpoint-authoritative memory;
- offline baselines for lexical knowledge survival and for per-generation
  behavioral adoption of the hidden rules (risky berry eats, deep digs);
- a static, escaped viewer export which never exposes raw reasoning.

The oral channel, a second valley, shocks, plague corruption, surveys and mixed
model populations are intentionally kept behind later versioned modules.  The
architecture asks that they be added one at a time only after the MVP produces
measurable selection.

## Security model

Legacy text is hostile input.  It is an intentional intergenerational prompt
injection, so prompt instructions and structured output alone are not a
security boundary.

Safe defaults are therefore fail-closed:

- no `shell=True`, dynamic command or inherited host environment;
- bounded UTF-8/JSON input and output with duplicate keys, NaN and unknown
  action fields rejected;
- per-invocation HOME/work directory and process-group timeout cleanup;
- per-process CPU, address-space, file, process and descriptor limits on
  supported Unix systems;
- no writable access from agents to the event log, SQLite or hidden rules;
- hash-chain and state-hash verification before resume;
- model text is escaped in the viewer and sanitized before operator logs.

`process` isolation is **not** presented as a strong sandbox.  Real agentic CLIs
must use the bubblewrap policy (or an equivalent container/microVM worker) with
an egress proxy that holds provider credentials.  Network inheritance and
host-process execution require explicit unsafe acknowledgement.  If the
requested boundary is unavailable, the adapter refuses to start.

Bubblewrap supplies namespace isolation but not aggregate resource accounting.
Production workers additionally need externally enforced cgroup limits.  Token
ceilings in this package are audit/fail-closed controls over adapter reports;
hard provider spend limits require host-owned metering and pre-dispatch
reservation at the credential-holding broker.

## Quick start

Requirements: Python 3.12+ and `uv`.

```bash
UV_CACHE_DIR=/tmp/uv-cache uv sync --extra dev
uv run terrarium run configs/mvp.yaml --output runs/mvp
uv run terrarium verify runs/mvp
uv run terrarium measure runs/mvp configs/mvp.yaml --output measurements/mvp
uv run pytest
```

## Scientific GUI

An optional loopback-only FastAPI + React interface provides schema-driven
configuration, managed start/stop/resume, committed-event SSE, read-only
telemetry, ECharts analysis views, and asynchronous audit tools.  GUI code is a
sibling package and does not modify the sealed `src/terrarium/` mechanics tree.

```bash
UV_CACHE_DIR=/tmp/terrarium-uv-cache uv sync --project gui --group dev
cd gui/frontend && npm ci && npm run build && cd ../..
uv run --project gui terrarium-gui --runs-root runs --configs-dir configs
```

Open the one-time loopback URL printed by the command.  Operational and
security details are in [`docs/gui.md`](docs/gui.md).

The checked-in MVP config uses deterministic mock agents and does not contact a
model provider.  A real adapter is a separate, pinned experiment configuration;
the executable digest, model ID and sandbox policy are sealed into the run
manifest and drift causes resume to fail.
The exact one-shot JSON contract is documented in
[`docs/adapter-protocol.md`](docs/adapter-protocol.md).
The subscription-compatible Claude Code runtime, checkpoint-authoritative memory,
and deliberately explicit host-execution boundary are documented in
[`docs/claude-code.md`](docs/claude-code.md), whose "First real-model run" runbook
walks through host prerequisites, tokenizer-cache seeding, and sealing a config.
`configs/pilot.yaml` enables lethal starvation and collapse and combines up to
three inherited records for a short selection-bearing rehearsal; it remains
mock-backed until its runtime section is intentionally replaced and sealed. To
seal a real Claude Code pilot against your host, run
`scripts/seal-claude-config.sh` (renders `configs/pilot-claude.yaml.template`
into a gitignored `configs/*.local.yaml`, pinning the resolved `claude` binary
and its sha256).
The mock validates the pipeline and safety invariants; it is not scientific
evidence that knowledge survived across generations.

This repository is therefore a safe engine/scaffold, not yet the complete
scientific protocol.  It does not ship an unsafe direct-Codex credential wrapper:
real model execution must be supplied as a pinned subprocess behind the documented
sandbox and credential-broker boundary.  The bundled lexical classifier is a
pipeline smoke signal, not the architecture's embedding-plus-NLI gate; scientific
claims require a separately versioned, pinned classifier and validation set.

## Data layout

```text
run/
  manifest.json       pinned experiment and runtime metadata
  events.jsonl        append-only, hash-chained source of truth
  state.sqlite3       rebuildable query/checkpoint projection
  raw/                optional private adapter cache, never served by viewer
```

Only a `tick_commit` after `fsync` makes a tick durable.  An incomplete final
tail is ignored on recovery; corruption in committed history is fatal.  The
measurement package reads a sealed run and writes to a separate output tree.
Optional raw files are deliberately non-authoritative and are not bound into the
event hash chain; retain them only as a private diagnostic cache.

## Development

```bash
uv run ruff check .
uv run pytest --cov=terrarium
```

The test suite covers action-order invariance, deterministic replay, in-place
corruption and partial-tail detection, SQLite rebuilds, malformed model JSON, secret-free
environments, process timeouts and immutable legacy provenance.  Strong network
and mount isolation tests require a Linux CI runner with bubblewrap available.
