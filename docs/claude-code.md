# Claude Code runtime

The `claude-code` runtime invokes the official Claude Code binary in headless
mode. Every call uses `--no-session-persistence` and receives the complete
checkpointed `AgentContext`.

Persistent Claude Code sessions are intentionally not used. They cannot join
Terrarium's atomic event-log commit: a timeout or crash can persist a provider
turn while its world tick remains uncommitted. Resuming that session would
silently diverge from replay, while recreating a deterministic session ID is
non-idempotent. The Terrarium checkpoint is therefore the sole lifetime memory.

This runtime is intentionally host-owned and is not the generic sandboxed
subprocess adapter. Run it as a dedicated OS user. Claude Code owns that user's
authentication and session files; the model itself receives no tools. Each call
uses an empty temporary working directory plus `--safe-mode`, empty setting
sources, strict empty MCP configuration, disabled slash commands, and `--tools
""`. The provider process still has host filesystem and network capabilities,
which is why the config requires explicit unsafe-host and network acknowledgement.

Do not add `--bare` when using subscription OAuth. Current Claude Code documents
and reports in `claude --help` that bare mode does not read OAuth or the keychain;
it accepts an API key or configured key helper instead.

Create a real config from `configs/pilot.yaml`, then replace the runtime section:

```yaml
runtime:
  adapter: claude-code
  argv: [/absolute/path/to/claude]
  executable_sha256: <sha256-of-the-resolved-claude-binary>
  provider: anthropic-claude-code
  model_id: sonnet
  timeout_seconds: 180
  max_input_bytes: 1048576
  max_output_bytes: 65536
  max_stderr_bytes: 16384
  sandbox:
    backend: process
    network: inherit
    acknowledge_unsafe_host_execution: true
```

Resolve and hash the exact executable rather than the shell shim or symlink:

```bash
readlink -f "$(command -v claude)"
sha256sum "$(readlink -f "$(command -v claude)")"
```

JSON output is schema-constrained by Claude Code and validated again by the
Terrarium boundary. Reported input, cache-read, cache-creation, and output tokens
feed the existing budget governor; `total_cost_usd` is retained as adapter
metadata. Missing usage fails closed. Provider-side context compaction and
external session deletion cannot affect replay.

For a scale rehearsal, start with the three-generation pilot and inspect mortality,
legacy text quality, failure rate, and token usage before increasing cohort count.
The lexical classifier remains a smoke test, not evidence-grade semantics.

## First real-model run

The checked configs (`mvp.yaml`, `pilot.yaml`) are mock-backed. Graduating to a
real Claude Code run is a distinct, sealed experiment. Follow this runbook.

### 1. Host prerequisites (operator-owned, not in this repo)

- Install and authenticate the `claude` binary as a **dedicated OS user**. That
  user owns OAuth/keychain; the model receives no tools (`--tools ""`,
  `--safe-mode`, empty setting sources).
- Because `sandbox.backend: process` + `network: inherit` give the provider
  process host filesystem and network capability, run behind an **egress proxy
  that holds the provider credentials** so they never enter the config or the
  agent environment. The token ceilings here are fail-closed *audit* controls;
  hard spend limits belong at the credential-holding broker (see `README.md`).

### 2. Seed the tokenizer cache (offline determinism)

`terrarium run` fetches `cl100k_base` on first use. Pin it to a persistent cache
so runs and replays are reproducible and offline-safe:

```bash
export TIKTOKEN_CACHE_DIR="$PWD/.tiktoken-cache"
uv run python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
```

### 3. Seal the config against this host

`runtime.argv` (absolute path to `claude`) and `runtime.executable_sha256` are
host-specific and must never be committed — `factory.py` verifies the digest and
fails the run on drift. Seal them from the committed template with the helper,
which renders, schema-validates, and verifies the digest offline:

```bash
scripts/seal-claude-config.sh          # writes configs/pilot-claude.local.yaml
```

Override the binary or paths with `--claude`, `--template`, `--output`. The
sealed `*.local.yaml` is gitignored. The manual equivalent is still the two
commands above (`readlink -f` + `sha256sum`) hand-edited into the template.

### 4. Run the pilot, then audit

```bash
uv run terrarium run configs/pilot-claude.local.yaml --output runs/pilot-claude
uv run terrarium verify runs/pilot-claude
uv run terrarium measure runs/pilot-claude configs/pilot-claude.local.yaml \
  --output measurements/pilot-claude
```

`measure` must write outside the run directory, and it requires the passed config
to byte-match the sealed one.

### 5. Graduation criteria before scaling

Inspect mortality, legacy text quality, failure rate, and token/cost usage from
the three-generation pilot before increasing `population.size` or
`population.generations`. The lexical classifier remains a smoke test, not
evidence-grade semantics — scientific claims require the separately versioned,
pinned embedding-plus-NLI classifier and validation set.
