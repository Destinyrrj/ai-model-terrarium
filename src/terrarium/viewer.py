"""Export an allowlisted, backend-free viewer without exposing raw responses."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Final

type _Sanitizer = Callable[[object], object]

_INVALID: Final = object()
_MAX_PUBLIC_EVENTS: Final = 100_000
_MAX_PUBLIC_JSON_BYTES: Final = 32 * 1024 * 1024
_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")


def _safe_text(value: object, *, max_chars: int) -> object:
    if not isinstance(value, str) or len(value) > max_chars:
        return _INVALID
    # textContent prevents markup execution; rejecting terminal and bidi controls
    # also prevents a public export from becoming a log/UI-spoofing surface.
    for character in value:
        if character not in {"\n", "\t"} and unicodedata.category(character) in {
            "Cc",
            "Cf",
            "Cs",
        }:
            return _INVALID
    return value


def _token(value: object) -> object:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        return _INVALID
    return value


def _short_text(value: object) -> object:
    return _safe_text(value, max_chars=512)


def _legacy_text(value: object) -> object:
    return _safe_text(value, max_chars=1_000_000)


def _nonnegative_int(value: object) -> object:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        return _INVALID
    return value


def _signed_int(value: object) -> object:
    if type(value) is not int or not -(2**63) <= value <= 2**63 - 1:
        return _INVALID
    return value


def _boolean(value: object) -> object:
    return value if type(value) is bool else _INVALID


def _digest(value: object) -> object:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        return _INVALID
    return value


def _token_list(value: object) -> object:
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        return _INVALID
    result: list[str] = []
    for item in value:
        sanitized = _token(item)
        if sanitized is _INVALID:
            return _INVALID
        assert isinstance(sanitized, str)
        result.append(sanitized)
    return result


def _action(value: object) -> object:
    """Return only the closed game-action schema, never arbitrary nested data."""

    if not isinstance(value, Mapping):
        return _INVALID
    allowed: dict[str, _Sanitizer] = {
        "agent_id": _token,
        "type": _token,
        "destination": _token,
        "resource": _token,
        "item": _token,
        "depth": _nonnegative_int,
    }
    sanitized = _project_fields(value, allowed)
    action_type = sanitized.get("type")
    if action_type not in {"noop", "move", "forage", "eat", "dig"}:
        return _INVALID
    required_by_type = {
        "noop": set(),
        "move": {"destination"},
        "forage": {"resource"},
        "eat": {"item"},
        "dig": {"depth"},
    }
    variant_fields = {"destination", "resource", "item", "depth"}
    if set(sanitized).intersection(variant_fields) != required_by_type[action_type]:
        return _INVALID
    if action_type == "dig" and not 1 <= sanitized["depth"] <= 10:
        return _INVALID
    if "agent_id" not in sanitized:
        return _INVALID
    return sanitized


def _project_fields(
    payload: Mapping[object, object],
    policy: Mapping[str, _Sanitizer],
) -> dict[str, object]:
    """Project known fields and drop a field completely when its value is invalid."""

    projected: dict[str, object] = {}
    for name, sanitizer in policy.items():
        if name not in payload:
            continue
        value = sanitizer(payload[name])
        if value is not _INVALID:
            projected[name] = value
    return projected


_LEGACY_FIELDS: Final[dict[str, _Sanitizer]] = {
    "id": _token,
    "legacy_id": _token,
    "author_agent_id": _token,
    "generation": _nonnegative_int,
    "generation_id": _token,
    "valley": _token,
    "channel": _token,
    "text": _legacy_text,
    "parent_legacy_ids": _token_list,
}
_DEATH_FIELDS: Final[dict[str, _Sanitizer]] = {
    "agent": _token,
    "agent_id": _token,
    "lineage_id": _token,
    "generation": _nonnegative_int,
    "generation_id": _token,
    "cause": _token,
    "location": _token,
    "age": _nonnegative_int,
}
_ACTION_FIELDS: Final[dict[str, _Sanitizer]] = {
    "action_id": _token,
    "agent": _token,
    "agent_id": _token,
    "action": _action,
    "valid": _boolean,
    "reason": _token,
    "error_code": _token,
    "replacement": _token,
}
_EFFECT_FIELDS: Final[dict[str, _Sanitizer]] = {
    "agent": _token,
    "agent_id": _token,
    "cause": _token,
    "reason": _token,
    "resource": _token,
    "location": _token,
    "before": _signed_int,
    "after": _signed_int,
    "amount": _nonnegative_int,
    "generation": _nonnegative_int,
}
_TICK_COMMIT_FIELDS: Final[dict[str, _Sanitizer]] = {
    "begin_seq": _nonnegative_int,
    "event_count": _nonnegative_int,
    "checkpoint_hash": _digest,
    "checkpoint_event_hash": _digest,
}
_EVENT_FIELDS: Final[dict[str, dict[str, _Sanitizer]]] = {
    "action": _ACTION_FIELDS,
    "action_validated": _ACTION_FIELDS,
    "intent": _ACTION_FIELDS,
    "invalid_action": _ACTION_FIELDS,
    "death": _DEATH_FIELDS,
    "death_recorded": _DEATH_FIELDS,
    "effect": _EFFECT_FIELDS,
    "shock": _EFFECT_FIELDS,
    "legacy": _LEGACY_FIELDS,
    "legacy_created": _LEGACY_FIELDS,
    "legacy_written": _LEGACY_FIELDS,
    "tick_commit": _TICK_COMMIT_FIELDS,
}

_INDEX = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none';
        script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self';
        base-uri 'none'; form-action 'none'; frame-ancestors 'none'">
  <title>AI Model Terrarium</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header><p class="eyebrow">SEALED RUN</p><h1>AI Model Terrarium</h1></header>
  <main>
    <section><h2>Run</h2><dl id="summary"></dl></section>
    <section><h2>Canon</h2><div id="canon" class="cards"></div></section>
    <section><h2>Timeline</h2><ol id="timeline"></ol></section>
  </main>
  <script src="app.js" defer></script>
</body>
</html>
"""

_APP_JS = """'use strict';

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

function addPair(parent, key, value) {
  parent.append(node('dt', key));
  parent.append(node('dd', value));
}

function render(data) {
  const summary = document.querySelector('#summary');
  addPair(summary, 'Run ID', data.run_id);
  addPair(summary, 'Events', data.events.length);
  addPair(summary, 'Last tick', data.last_tick);

  const canon = document.querySelector('#canon');
  const legacies = data.events.filter((event) =>
    event.type === 'legacy' || event.type === 'legacy_created' ||
    event.type === 'legacy_written');
  for (const event of legacies) {
    const card = node('article', undefined, 'card');
    card.append(node('p', `Generation ${event.payload.generation ?? '?'}`, 'meta'));
    card.append(node('p', event.payload.text ?? ''));
    card.append(node('code', event.payload.id ?? event.payload.legacy_id ?? 'unknown'));
    canon.append(card);
  }
  if (legacies.length === 0) canon.append(node('p', 'No written legacy yet.', 'muted'));

  const timeline = document.querySelector('#timeline');
  for (const event of data.events) {
    const item = node('li');
    item.append(node('span', `T${event.tick}`, 'tick'));
    item.append(node('strong', event.type));
    const detail = event.payload.agent_id ?? event.payload.agent ?? event.payload.cause ?? '';
    if (detail) item.append(node('span', detail));
    timeline.append(item);
  }
}

fetch('data.json', {cache: 'no-store', credentials: 'omit'})
  .then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  })
  .then(render)
  .catch((error) => {
    document.querySelector('main').replaceChildren(node('p', `Viewer error: ${error.message}`));
  });
"""

_STYLES = """:root {
  color-scheme:dark; font-family:Inter,ui-sans-serif,system-ui,sans-serif;
  background:#0b0d0c; color:#dce5df;
}
* { box-sizing:border-box; }
body {
  margin:0; min-height:100vh;
  background:radial-gradient(circle at 12% 0%,#183127 0,transparent 34rem),#0b0d0c;
}
header, main { width:min(72rem,calc(100% - 2rem)); margin:auto; }
header { padding:5rem 0 2rem; border-bottom:1px solid #355044; }
h1 { margin:.2rem 0; font-size:clamp(2.5rem,8vw,6rem); letter-spacing:-.06em; }
h2 { font-size:1rem; letter-spacing:.14em; text-transform:uppercase; color:#9cbaa9; }
.eyebrow,.meta,.muted { color:#7f9a8b; }
main {
  display:grid; grid-template-columns:minmax(14rem,1fr) minmax(0,2.4fr);
  gap:2rem; padding:2rem 0 6rem;
}
section:last-child { grid-column:1/-1; }
dl { display:grid; grid-template-columns:auto 1fr; gap:.6rem 1rem; }
dt { color:#779183; } dd { margin:0; overflow-wrap:anywhere; }
.cards { display:grid; gap:.8rem; }
.card { padding:1rem; border:1px solid #2b4538; background:#111815; border-radius:.6rem; }
.card p { white-space:pre-wrap; overflow-wrap:anywhere; }
code { color:#a9d9bc; }
ol { list-style:none; padding:0; display:grid; gap:.35rem; }
li {
  display:grid; grid-template-columns:5rem 12rem 1fr; gap:.75rem;
  padding:.45rem .6rem; border-left:2px solid #355c48; background:#0e1311;
}
.tick { color:#7f9a8b; font-variant-numeric:tabular-nums; }
@media (max-width:48rem) {
  main { grid-template-columns:1fr; }
  section:last-child { grid-column:auto; }
  li { grid-template-columns:4rem 1fr; }
  li span:last-child { grid-column:2; }
}
"""


def _safe_event(event: Mapping[str, object]) -> dict[str, object] | None:
    event_type = event.get("type")
    tick = event.get("tick")
    payload = event.get("payload")
    if not isinstance(event_type, str) or event_type not in _EVENT_FIELDS:
        return None
    if type(tick) is not int or not 0 <= tick <= 2**63 - 1:
        return None
    if not isinstance(payload, Mapping):
        return None
    public_payload = _project_fields(payload, _EVENT_FIELDS[event_type])

    # A malformed known field must not let an apparently valid record through by
    # disappearing during projection.  These are the minimum display identities.
    if event_type in {"legacy", "legacy_created"}:
        if not {"id", "generation", "text"}.issubset(public_payload):
            return None
    elif event_type == "legacy_written":
        if not {"legacy_id", "author_agent_id", "generation", "text"}.issubset(
            public_payload
        ):
            return None
    elif event_type in {"death", "death_recorded"}:
        if "agent_id" not in public_payload or "cause" not in public_payload:
            return None
    elif event_type in {"intent", "invalid_action"}:
        if not {"action_id", "agent_id", "action", "valid"}.issubset(public_payload):
            return None
        action = public_payload["action"]
        assert isinstance(action, dict)
        if action.get("agent_id") != public_payload["agent_id"]:
            return None
        if event_type == "intent" and public_payload["valid"] is not True:
            return None
        if event_type == "invalid_action" and public_payload["valid"] is not False:
            return None
    return {"tick": tick, "type": event_type, "payload": public_payload}


def export_viewer(
    events: Iterable[Mapping[str, object]],
    *,
    run_id: str,
    output_dir: str | Path,
) -> None:
    destination = Path(output_dir).absolute()
    public_events: list[dict[str, object]] = []
    for event in events:
        safe = _safe_event(event)
        if safe is None:
            continue
        if len(public_events) >= _MAX_PUBLIC_EVENTS:
            raise ValueError("viewer export exceeds the public event limit")
        public_events.append(safe)
    last_tick = max((int(event["tick"]) for event in public_events), default=-1)
    data = {"schema_version": 1, "run_id": run_id, "last_tick": last_tick, "events": public_events}
    if _token(run_id) is _INVALID:
        raise ValueError("viewer run_id is invalid")
    encoded_data = (
        json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(encoded_data) > _MAX_PUBLIC_JSON_BYTES:
        raise ValueError("viewer export exceeds the public byte limit")

    directory_fd = _open_output_directory(destination)
    try:
        # data.json is the commit marker: publish immutable assets first, then the
        # complete bounded data document.  Each replacement overwrites a symlink
        # itself instead of following it to an arbitrary target.
        _atomic_write(directory_fd, "index.html", _INDEX.encode("utf-8"))
        _atomic_write(directory_fd, "app.js", _APP_JS.encode("utf-8"))
        _atomic_write(directory_fd, "styles.css", _STYLES.encode("utf-8"))
        _atomic_write(directory_fd, "data.json", encoded_data)
    finally:
        os.close(directory_fd)


def _open_output_directory(destination: Path) -> int:
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    info = destination.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("viewer output must be a real directory")
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | int(
        getattr(os, "O_CLOEXEC", 0)
    )
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    directory_fd = os.open(destination, flags)
    opened = os.fstat(directory_fd)
    if not stat.S_ISDIR(opened.st_mode):
        os.close(directory_fd)
        raise ValueError("viewer output must be a real directory")
    return directory_fd


def _atomic_write(directory_fd: int, name: str, data: bytes) -> None:
    temporary = f".{name}.{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(temporary, flags, 0o644, dir_fd=directory_fd)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(fd)
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary = ""
        os.fsync(directory_fd)
    finally:
        os.close(fd)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
