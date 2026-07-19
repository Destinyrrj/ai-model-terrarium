# Subprocess adapter protocol

This protocol is the only model-facing channel.  An adapter is a one-shot
process: it reads one bounded UTF-8 JSON object from standard input, writes one
bounded JSON object to standard output, and exits.  It never receives a path to
the repository, run directory, event log, database or hidden-rule source.

## Request

The request has exactly two top-level fields:

```json
{"operation":"act","payload":{}}
```

`operation` is one of:

| Operation | Payload | Successful response payload |
| --- | --- | --- |
| `act` | The complete dry `AgentContext.act_envelope()` | One strict `Action` object |
| `write_legacy` | `{"budget_tokens":N,"context":DEATHBED_CONTEXT}` | `{"text":"..."}` |
| `retell` | A structured legacy record | `{"text":"..."}` |
| `answer_survey` | A structured out-of-world probe | Provider-specific structured answers |

The written-channel MVP invokes only `act` and `write_legacy`.  An action is a
closed variant (`noop`, `move`, `forage`, `eat` or `dig`) and has no prose,
command, path, URL, tool or reasoning field.  Unknown fields are rejected and
become an audited no-op at the orchestration boundary.

## Response

A real adapter should return this exact envelope:

```json
{
  "payload": {"agent_id":"agent-0","type":"noop"},
  "usage": {"input_tokens":12,"output_tokens":8,"total_tokens":20}
}
```

Only `payload` and `usage` are allowed.  `input_tokens` and `output_tokens` are
required non-negative integers; `total_tokens`, when present, must equal their
sum.  A bare object is accepted as a compatibility payload but has unknown
usage, so a governed experiment pauses fail-closed rather than treating it as
zero-cost inference.

Standard output must contain exactly one finite JSON value: duplicate keys,
NaN/infinity, trailing text, excessive nesting and oversized output are
rejected.  Standard error is never interpreted as an action and is only kept as
a bounded, terminal-sanitized diagnostic.  Raw responses, when explicitly
enabled, are private diagnostic-cache files and are never exported by the viewer.
They are not bound into the authoritative event hash chain; see `SECURITY.md`.

## Security deployment contract

The executable and argv are fixed by configuration and the executable digest
is sealed in the manifest.  `shell=True`, PATH lookup and host-environment
inheritance are forbidden.  The safe real-process backend requires bubblewrap
with a pinned binary digest, a minimal read-only system root, private HOME/cwd,
no run-directory mount and default-deny networking.

Provider access must terminate at an externally enforced egress proxy which
owns credentials; proxy environment variables alone are not a network
boundary.  Aggregate CPU, memory and PID control likewise requires an external
cgroup/container/microVM quota.  The `process` backend is an explicitly
acknowledged development mode, not a security sandbox.

The repository provides this generic one-shot JSON bridge and a deterministic
mock, not a direct Codex-CLI credential integration.  A production integration
must preserve the same closed response schema while keeping credentials and
provider metering in the host-owned broker boundary.
