# Claude Code runtime

The `claude-code` runtime invokes the official Claude Code binary in headless
mode. One Terrarium agent maps to one deterministic Claude Code session. The
first action uses `--session-id`; later actions and the deathbed use `--resume`.
This also survives a Terrarium checkpoint/resume, provided Claude Code has kept
the session in the same OS user's account.

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
metadata. Missing usage fails closed. A missing or externally compacted/deleted
Claude session is an audited adapter failure, not a silent stateless fallback.

For a scale rehearsal, start with the three-generation pilot and inspect mortality,
legacy text quality, failure rate, and token usage before increasing cohort count.
The lexical classifier remains a smoke test, not evidence-grade semantics.
