# Security model

## Scope

The simulator intentionally feeds model-written legacy text to later models.
That text is untrusted prompt injection by design.  The security objective is
to let it influence game decisions while preventing it from affecting the host,
the world engine, other agent sandboxes, credentials or durable evidence.

## Trust boundaries

1. Orchestrator to adapter process: arbitrary model/CLI behavior, attempted
   resource exhaustion and data exfiltration are in scope.  Code-level limits
   are per process; aggregate exhaustion is an infrastructure boundary below.
2. Agent payload to world: malformed JSON and attempts to smuggle commands or
   hidden state are in scope.
3. Event log to SQLite: torn writes, replay and in-place historical-byte
   corruption are in scope.
4. Model text to viewer/operator terminal: XSS, ANSI/OSC, bidi and log-forging
   payloads are in scope.
5. Experiment configuration and binaries: silent model, policy, executable or
   dependency drift is in scope.

The model provider itself and kernel/virtualization zero-days are outside the
code-only threat model.  Production deployments should treat those as
infrastructure risks and layer a microVM or hardened container boundary.

## Enforced controls

- strict Pydantic actions with unknown fields forbidden; invalid output is a
  typed no-op;
- no shell command construction; executable argv and binary hash are pinned;
- clean environment, private ephemeral HOME/cwd/tmp and bounded standard I/O;
- timeout cleanup for the entire process group plus per-process Unix resource
  limits;
- bubblewrap backend is network-off by default; inherited networking requires
  an explicit unsafe acknowledgement;
- append-only canonical events with sequence and SHA-256 predecessor hashes;
- a tick is durable only after its commit marker is flushed and `fsync`ed;
- SQLite uses one writer, WAL, `synchronous=FULL` and idempotent projections;
- immutable legacy provenance and state hashes at every checkpoint;
- raw files are fixed-root, opaque-path, no-follow and mode 0600;
- static viewer exports allowlisted fields only, renders via `textContent`, and
  ships a restrictive CSP;
- run manifests seal config, model, Python, NumPy/PCG64, executable and sandbox
  policy identities.  Drift refuses resume.

The local SHA-256 chain is not an external trust anchor.  A host operator with
write access to the whole run directory can truncate it to an older committed
head or rewrite the log and recompute hashes.  Adversarial rollback protection
requires persisting/signed anchoring of `{run_id, last_seq, last_hash}` in an
append-only system outside `run/`.  Against model agents this is separated by
the sandbox boundary: agents receive no run-directory mount or storage API.

Executable/package digests are startup drift checks, not a cure for host-level
TOCTOU.  A privileged local actor could replace a path or an interpreter-loaded
dependency after verification.  Production workers should execute from an
immutable, digest-addressed image (or an already-open verified descriptor with
an equally sealed dependency tree).  Model agents cannot exercise this race
because they receive neither host paths nor a writable host mount.

## Strong sandbox requirement

The `process` backend provides failure containment, **not** a security boundary.
It shares the host kernel, filesystem namespace and network namespace.  It is
disabled unless `acknowledge_unsafe_host_execution` is true.

For an actual agentic CLI, use bubblewrap or a separate rootless
container/microVM worker with:

- a minimal read-only root filesystem and no repository, run-log, DB, Docker
  socket, SSH config, cloud metadata or other-agent mounts;
- unique UID/HOME/cache/tmp and separate PID, IPC and mount namespaces;
- no-new-privileges, dropped capabilities, seccomp/AppArmor and cgroup limits;
- provider-only egress through a filtering proxy which owns the credential;
- localhost, RFC1918, link-local/metadata and arbitrary DNS blocked;
- no model tools except the explicitly defined game-action output channel.

If these controls are unavailable, run the deterministic mock population.  Do
not weaken the policy silently to keep an experiment running.

`provider-proxy` is accepted only when the operator attests that an external
network namespace/firewall actually enforces that route.  Proxy environment
variables alone are bypassable and are never treated as a boundary.

Bubblewrap alone does not provide an aggregate CPU, memory or PID budget.
`RLIMIT_*` values apply per process (and some limits have host-UID semantics),
so a production worker must also attest an externally enforced cgroup or
equivalent container/microVM quota.  Without that layer the sandbox protects
mount/network separation but is not claimed to contain aggregate denial of
service.

## Budget boundary

The call count is reserved atomically before dispatch and restored from the
latest committed checkpoint.  A crash during an uncommitted inference batch can
cause that batch to be retried, so it is not a durable billing ledger.  Token
counts arrive from the subprocess after a call and are not trusted cost metering.
Missing or invalid usage pauses the run, and reported totals crossing a ceiling
pause future progress, but concurrent calls may already be in flight and a
malicious CLI can report zero.  Enforcing a provider-spend cap requires a
credential-holding broker that measures usage itself and reserves known input
plus configured worst-case output before dispatch.

## Data handling

Raw output can contain sensitive or provider-controlled data.  It is disabled
in the checked-in config.  When enabled, give the run directory a quota and a
retention policy, restrict it to the experiment operator, and never publish it
through the viewer.  Provider secrets must not appear in argv, prompts, model
environment, events or raw output.

The optional `raw/` tree is a non-authoritative diagnostic cache: the hash-chained
event records only whether capture succeeded, not the raw filename or content
digest.  `verify` therefore does not prove raw-file presence or detect replacement
and must not be cited as raw-evidence verification.  Experiments that need raw
responses as evidence should export them to a separately access-controlled,
content-addressed/WORM store and anchor that store's manifest externally.

## Reporting

Do not open a public issue for a vulnerability which could expose credentials
or escape a configured sandbox.  Use GitHub's private vulnerability reporting
for the repository owner and include the policy, platform and smallest safe
reproducer.
