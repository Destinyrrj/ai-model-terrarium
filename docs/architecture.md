# Architecture and invariants

## Experimental invariant

No model call participates in world mechanics.  The only planned exception is
the future oral-legacy channel, where distortion by the retelling model is the
measured treatment itself.

The engine is a pure transition over prior state, a canonical batch of accepted
actions and the serialized PCG64 state.  Adapter completion order, wall-clock
time, provider latency and prose never enter the transition.

## Tick transaction

1. The orchestrator creates dry, personal observations from committed state.
2. It reserves budget and calls every live adapter concurrently with a timeout.
3. Raw bounded output may be stored outside the event stream; strict payloads
   are validated into domain actions.
4. Missing, invalid, late or oversized output becomes `noop` and an audit event.
5. Actions are sorted by agent/resource key.  The engine resolves conflicts and
   hidden rules using only its PCG64 generator.
6. Engine events, effects and personal observations are canonically hashed.
7. Deathbed calls and replacement agents run as explicit lifecycle operations;
   every RNG draw and spawn is replayable, and a failed deathbed creates no
   empty legacy.
8. The complete event batch and post-lifecycle checkpoint are canonicalized and
   linked to the previous event hash.
9. JSONL records are appended, a `tick_commit` is flushed and `fsync`ed, then an
   idempotent SQLite transaction catches the projection up.

On recovery, an incomplete final JSONL tail is ignored.  A chain error anywhere
in committed history stops the run.  SQLite can be deleted and rebuilt from the
log without changing the latest checkpoint.

## Data ownership

- `WorldEngine`: locations, resources, weather, health, hunger, age, hidden
  rules, simultaneous effects and the sole RNG.
- `AgentAdapter`: inference only; returns untrusted structured data.
- `Orchestrator`: concurrency, lifecycle, budgets and commit ordering.
- `InheritanceManager`: immutable, tokenizer-bounded texts and provenance; it
  receives RNG draws from the engine rather than owning a generator.
- `EventStore`: durable canonical truth and rebuildable query projection.
- `measurement`: read-only offline classification and survival curves.
- `viewer`: allowlisted static projection, never raw responses.

## Reproducibility levels

`seed + config` cannot force a remote language model to repeat its outputs.
Reproducibility therefore has two explicit levels:

- world replay: pinned manifest + accepted actions reproduce mechanics, output
  hashes and each explicit lifecycle RNG/spawn operation before the checkpoint
  is accepted;
- inference rerun: best-effort only, additionally pinned by model ID, executable,
  tokenizer, prompt template, sandbox policy and dependency manifest.

Model switching is an experimental intervention and must start a new manifest
or be represented by a versioned event.  The budget governor never changes a
population silently.

## Increment order

The current package targets the written-channel MVP.  After its scientific gate
passes, extensions should land one at a time: oral channel, second valley,
shocks, plague corruption, surveys, mixed populations, then steganography.  A
new module must not alter historical event semantics without a schema version.
