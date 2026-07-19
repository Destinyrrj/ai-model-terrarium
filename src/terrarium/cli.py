"""Small, JSON-only command line interface for sealed Terrarium runs."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import secrets
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path

from pydantic import BaseModel, ValidationError

from . import storage as storage_module
from .config import RunConfig, load_config
from .domain import Action
from .factory import build_adapter_factory
from .manifest import RunManifest
from .measurement import (
    LEXICAL_BASELINE_ID,
    LexicalKnowledgeClassifier,
    MeasurementBasis,
    authored_measurement_records,
    behavior_adoption_curve,
    extract_inherited_exposures,
    extract_legacies,
    iter_committed_events,
    knowledge_survival_curve,
    write_behavior_measurements,
    write_measurements,
)
from .replay import ReplayError, load_manifest_config, replay_run
from .storage import EVENT_LOG_NAME, EventStore, StorageError
from .viewer import export_viewer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terrarium",
        description="Run and audit deterministic AI Model Terrarium experiments.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="start or resume an experiment")
    run.add_argument("config", type=Path)
    run.add_argument("--output", required=True, type=Path, metavar="RUN_DIR")
    run.add_argument("--max-ticks", type=_nonnegative_int)
    run.set_defaults(handler=_command_run)

    verify = subparsers.add_parser("verify", help="verify log and SQLite integrity")
    verify.add_argument("run_dir", type=Path)
    verify.set_defaults(handler=_command_verify)

    rebuild = subparsers.add_parser("rebuild", help="rebuild SQLite from committed JSONL")
    rebuild.add_argument("run_dir", type=Path)
    rebuild.set_defaults(handler=_command_rebuild)

    measure = subparsers.add_parser(
        "measure",
        help="write offline lexical-baseline knowledge measurements",
    )
    measure.add_argument("run_dir", type=Path)
    measure.add_argument("config", type=Path)
    measure.add_argument("--output", required=True, type=Path, metavar="OUTPUT_DIR")
    measure.set_defaults(handler=_command_measure)

    viewer = subparsers.add_parser("viewer", help="export an allowlisted static viewer")
    viewer.add_argument("run_dir", type=Path)
    viewer.add_argument("--output", required=True, type=Path, metavar="OUTPUT_DIR")
    viewer.set_defaults(handler=_command_viewer)

    replay = subparsers.add_parser("replay", help="re-execute recorded world mechanics")
    replay.add_argument("run_dir", type=Path)
    replay.set_defaults(handler=_command_replay)

    schema = subparsers.add_parser("schema", help="emit a public JSON schema")
    schema.add_argument(
        "target",
        nargs="?",
        choices=("all", "config", "action", "manifest"),
        default="all",
    )
    schema.set_defaults(handler=_command_schema)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        result = args.handler(args)
        _emit(result)
        return 0
    except (ValidationError, ValueError) as exc:
        # Configuration, schema and output-path failures can contain arbitrary
        # user input in their exception text.  Return only a stable category.
        _emit_error("validation_failed", type(exc).__name__)
    except ReplayError as exc:
        _emit_error("replay_failed", type(exc).__name__)
    except StorageError as exc:
        _emit_error("integrity_failed", type(exc).__name__)
    except (OSError, RuntimeError) as exc:
        _emit_error("operation_failed", type(exc).__name__)
    return 1


def _command_run(args: argparse.Namespace) -> object:
    config = load_config(args.config)
    output = _run_output_path(args.output)
    adapter_factory = build_adapter_factory(config)
    # Import lazily so read-only audit commands do not import adapter/orchestration
    # machinery and remain usable for incident recovery.
    from .orchestrator import ExperimentRunner

    runner = ExperimentRunner(config, output, adapter_factory=adapter_factory)

    async def execute() -> object:
        try:
            return await runner.run(max_ticks=args.max_ticks)
        finally:
            await runner.close()

    return asyncio.run(execute())


def _command_verify(args: argparse.Namespace) -> dict[str, object]:
    root, config = _sealed_run(args.run_dir)
    with _event_store(root, config) as store:
        report = store.verify()
    return {"command": "verify", "status": "ok", **report}


def _command_rebuild(args: argparse.Namespace) -> dict[str, object]:
    root, config = _sealed_run(args.run_dir)
    report = _recover_sqlite_projection(root, config)
    return {"command": "rebuild", "status": "ok", **report}


def _command_measure(args: argparse.Namespace) -> dict[str, object]:
    root, sealed = _sealed_run(args.run_dir)
    requested = load_config(args.config)
    _require_same_config(sealed, requested)
    destination = _separate_output_path(args.output, root)
    events = _verified_events(root, sealed)
    legacies = extract_legacies(events)
    inherited = extract_inherited_exposures(events, legacies)
    records = [*authored_measurement_records(legacies), *inherited]
    points = knowledge_survival_curve(
        records,
        sealed.knowledge,
        LexicalKnowledgeClassifier(),
        expected_generations=range(sealed.population.generations),
        expected_channels=("written",),
        expected_bases=tuple(MeasurementBasis),
    )
    behavior_points = behavior_adoption_curve(
        events,
        expected_generations=range(sealed.population.generations),
    )
    write_measurements(points, destination)
    write_behavior_measurements(behavior_points, destination)
    return {
        "command": "measure",
        "status": "ok",
        "run_id": sealed.run_id,
        "classifier": LEXICAL_BASELINE_ID,
        "legacies": len(legacies),
        "inherited_exposures": len(inherited),
        "points": len(points),
        "behavior_points": len(behavior_points),
        "output": str(destination),
    }


def _command_viewer(args: argparse.Namespace) -> dict[str, object]:
    root, config = _sealed_run(args.run_dir)
    destination = _separate_output_path(args.output, root)
    events = _verified_events(root, config)
    export_viewer(events, run_id=config.run_id, output_dir=destination)
    return {
        "command": "viewer",
        "status": "ok",
        "run_id": config.run_id,
        "events_read": len(events),
        "output": str(destination),
    }


def _command_replay(args: argparse.Namespace) -> dict[str, object]:
    return {"command": "replay", **replay_run(args.run_dir).to_dict()}


def _command_schema(args: argparse.Namespace) -> dict[str, object]:
    schemas: dict[str, dict[str, object]] = {
        "config": RunConfig.model_json_schema(),
        "action": Action.model_json_schema(),
        "manifest": RunManifest.model_json_schema(),
    }
    selected = schemas if args.target == "all" else {args.target: schemas[args.target]}
    return {"schema_version": 1, "schemas": selected}


def _verified_events(root: Path, config: RunConfig) -> list[dict[str, object]]:
    with _event_store(root, config) as store:
        before = store.verify()
        events = list(iter_committed_events(root / EVENT_LOG_NAME))
        after = store.verify()
    identity_keys = ("committed_events", "last_seq", "last_hash")
    if tuple(before.get(key) for key in identity_keys) != tuple(
        after.get(key) for key in identity_keys
    ):
        raise StorageError("durable log changed during read")
    if len(events) != before["committed_events"]:
        raise StorageError("committed event read length mismatch")
    if events and (
        events[-1].get("seq") != before["last_seq"] or events[-1].get("hash") != before["last_hash"]
    ):
        raise StorageError("committed event read identity mismatch")
    return events


def _event_store(root: Path, config: RunConfig) -> EventStore:
    return EventStore(
        root,
        config.run_id,
        max_raw_bytes=max(1, config.storage.max_raw_bytes),
    )


def _recover_sqlite_projection(root: Path, config: RunConfig) -> dict[str, object]:
    """Rebuild SQLite without trusting or opening the existing projection.

    ``EventStore.__init__`` normally projects the log immediately, which is the
    right fail-closed behavior for a runner but prevents a recovery command from
    opening a conflicting database.  This path holds the same writer lock,
    verifies JSONL with EventStore's scanner, constructs a fresh projection, and
    atomically swaps it in.  The old private files are retained under recovery/.
    """

    lock_fd = storage_module._secure_regular_file(root / storage_module.LOCK_NAME)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise storage_module.StoreLockedError(f"run already has a writer: {root}") from exc
        scanner = object.__new__(EventStore)
        scanner.run_id = config.run_id
        scanner.event_log_path = root / EVENT_LOG_NAME
        scanner.max_event_line_bytes = storage_module.DEFAULT_MAX_EVENT_LINE_BYTES
        scan = scanner._scan_log()
        if scan.incomplete_tail_bytes:
            raise storage_module.StorageIntegrityError(
                "refusing projection rebuild while JSONL has an incomplete tail"
            )

        token = secrets.token_hex(16)
        temporary = root / f".{storage_module.SQLITE_NAME}.cli-rebuild-{token}"
        connection = None
        try:
            connection = storage_module._open_sqlite(temporary, wal=False)
            scanner._initialize_schema(connection)
            scanner._bind_database_to_run(connection)
            scanner._project_transactions(connection, scan.transactions)
            scanner._conn = connection
            scanner._verify_sqlite(scan)
            connection.close()
            connection = None
            _fsync_regular_file(temporary)

            backups = _backup_projection_files(root, token)
            sqlite_path = root / storage_module.SQLITE_NAME
            try:
                os.replace(temporary, sqlite_path)
                os.chmod(sqlite_path, 0o600)
                storage_module._fsync_directory(root)
                connection = storage_module._open_sqlite(sqlite_path, wal=True)
                scanner._conn = connection
                scanner._verify_sqlite(scan)
            except BaseException:
                if connection is not None:
                    connection.close()
                    connection = None
                _restore_projection_backup(root, backups, token)
                raise
            finally:
                if connection is not None:
                    connection.close()
                    connection = None
        finally:
            if connection is not None:
                connection.close()
            storage_module._safe_unlink(temporary)
            storage_module._safe_unlink(Path(f"{temporary}-journal"))

        return {
            "run_id": config.run_id,
            "schema_version": 1,
            "committed_ticks": len(scan.transactions),
            "committed_events": len(scan.events),
            "last_tick": scan.last_tick,
            "last_seq": scan.last_seq,
            "last_hash": scan.last_hash,
            "incomplete_tail_bytes": scan.incomplete_tail_bytes,
            "sqlite": "ok",
        }
    finally:
        os.close(lock_fd)


def _backup_projection_files(root: Path, token: str) -> dict[Path, Path]:
    recovery = storage_module._ensure_directory(root / storage_module.RECOVERY_DIRECTORY_NAME)
    backups: dict[Path, Path] = {}
    sqlite_path = root / storage_module.SQLITE_NAME
    sources: list[Path] = []
    for source in (sqlite_path, Path(f"{sqlite_path}-wal"), Path(f"{sqlite_path}-shm")):
        try:
            info = source.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise storage_module.StorageIntegrityError("unsafe SQLite projection path")
        sources.append(source)

    try:
        for source in sources:
            os.chmod(source, 0o600)
            destination = recovery / f"{source.name}.pre-rebuild-{token}"
            os.replace(source, destination)
            backups[source] = destination
    except BaseException:
        for original, backup in reversed(tuple(backups.items())):
            os.replace(backup, original)
        storage_module._fsync_directory(root)
        storage_module._fsync_directory(recovery)
        raise
    storage_module._fsync_directory(root)
    storage_module._fsync_directory(recovery)
    return backups


def _restore_projection_backup(root: Path, backups: Mapping[Path, Path], token: str) -> None:
    sqlite_path = root / storage_module.SQLITE_NAME
    recovery = root / storage_module.RECOVERY_DIRECTORY_NAME
    for current in (sqlite_path, Path(f"{sqlite_path}-wal"), Path(f"{sqlite_path}-shm")):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise storage_module.StorageIntegrityError("unsafe failed SQLite projection path")
        failed = recovery / f"{current.name}.failed-rebuild-{token}"
        os.replace(current, failed)
    for destination, backup in backups.items():
        os.replace(backup, destination)
    storage_module._fsync_directory(root)
    storage_module._fsync_directory(recovery)


def _fsync_regular_file(path: Path) -> None:
    flags = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise storage_module.StorageIntegrityError("rebuilt SQLite is not a private file")
        os.fsync(fd)
    finally:
        os.close(fd)


def _sealed_run(path: str | Path) -> tuple[Path, RunConfig]:
    root = _existing_directory(path, "run")
    return root, load_manifest_config(root)


def _require_same_config(sealed: RunConfig, requested: RunConfig) -> None:
    if sealed.canonical_bytes() != requested.canonical_bytes():
        raise ValueError("measurement configuration differs from sealed run")


def _run_output_path(path: str | Path) -> Path:
    candidate = Path(path).absolute()
    if candidate.exists() or candidate.is_symlink():
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("run output must be a real directory")
        return candidate.resolve(strict=True)
    parent = candidate.parent.resolve(strict=True)
    return parent / candidate.name


def _separate_output_path(path: str | Path, run_root: Path) -> Path:
    candidate = Path(path).absolute()
    if candidate.exists() or candidate.is_symlink():
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("output must be a real directory")
        resolved = candidate.resolve(strict=True)
    else:
        parent = candidate.parent.resolve(strict=True)
        resolved = parent / candidate.name
    if resolved == run_root or run_root in resolved.parents:
        raise ValueError("offline output must be outside the sealed run directory")
    return resolved


def _existing_directory(path: str | Path, name: str) -> Path:
    candidate = Path(path).absolute()
    try:
        info = candidate.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{name} directory does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{name} path must be a real directory")
    return candidate.resolve(strict=True)


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _emit(value: object) -> None:
    serializable = _jsonable(value)
    sys.stdout.write(
        json.dumps(serializable, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    )


def _emit_error(code: str, exception_type: str) -> None:
    # Class names are code-owned ASCII identifiers; exception messages are not.
    payload = {
        "status": "error",
        "error": code,
        "exception_type": exception_type,
    }
    sys.stderr.write(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    raise TypeError(f"unsupported command result type: {type(value).__name__}")


if __name__ == "__main__":  # pragma: no cover - console entry point is primary
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
