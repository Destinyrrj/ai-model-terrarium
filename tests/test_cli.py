from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from terrarium.cli import main
from terrarium.config import RunConfig

from .test_replay import _config, _sealed_log


def _result(capsys: object) -> dict[str, object]:
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert captured.err == ""
    value = json.loads(captured.out)
    assert isinstance(value, dict)
    return value


def test_schema_command_is_json(capsys: object) -> None:
    assert main(["schema", "action"]) == 0
    result = _result(capsys)
    assert result["schema_version"] == 1
    assert set(result["schemas"]) == {"action"}  # type: ignore[arg-type]


def test_mock_run_smoke_and_replay(tmp_path: Path, capsys: object) -> None:
    config_data = _config("cli-mock-run").model_dump(mode="json")
    config_data["population"]["lifespan_ticks"] = 1
    config = RunConfig.model_validate(config_data)
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    run_dir = tmp_path / "run"

    assert (
        main(
            [
                "run",
                str(config_path),
                "--output",
                str(run_dir),
                "--max-ticks",
                "1",
            ]
        )
        == 0
    )
    result = _result(capsys)
    assert result["ticks_run"] == 1
    assert result["world_tick"] == 1
    assert result["completed"] is True

    assert main(["verify", str(run_dir)]) == 0
    assert _result(capsys)["sqlite"] == "ok"
    assert main(["replay", str(run_dir)]) == 0
    assert _result(capsys)["ticks_replayed"] == 1

    measurements = tmp_path / "mock-measurements"
    assert (
        main(
            [
                "measure",
                str(run_dir),
                str(config_path),
                "--output",
                str(measurements),
            ]
        )
        == 0
    )
    measured = _result(capsys)
    assert measured["classifier"] == "lexical-keyword-baseline-v1"
    assert measured["legacies"] == 2
    assert measured["inherited_exposures"] == 4
    assert measured["points"] == 12

    rows = json.loads((measurements / "knowledge-survival.json").read_text(encoding="utf-8"))
    berries = [row for row in rows if row["fact_id"] == "red_berries_after_rain"]
    indexed = {(row["basis"], row["generation"]): row for row in berries}
    assert indexed[("authored", 0)]["total_legacies"] == 2
    assert indexed[("authored", 1)]["total_legacies"] == 0
    assert indexed[("inherited", 0)]["total_legacies"] == 0
    assert indexed[("inherited", 1)]["total_legacies"] == 4


def test_audit_export_commands_smoke(tmp_path: Path, capsys: object) -> None:
    run_dir = tmp_path / "run"
    config = _sealed_log(run_dir)
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")

    assert main(["verify", str(run_dir)]) == 0
    assert _result(capsys)["status"] == "ok"

    assert main(["rebuild", str(run_dir)]) == 0
    assert _result(capsys)["status"] == "ok"

    measurements = tmp_path / "measurements"
    assert (
        main(
            [
                "measure",
                str(run_dir),
                str(config_path),
                "--output",
                str(measurements),
            ]
        )
        == 0
    )
    assert _result(capsys)["status"] == "ok"
    assert (measurements / "knowledge-survival.json").is_file()

    viewer = tmp_path / "viewer"
    assert main(["viewer", str(run_dir), "--output", str(viewer)]) == 0
    assert _result(capsys)["status"] == "ok"
    assert (viewer / "index.html").is_file()

    assert main(["replay", str(run_dir)]) == 0
    assert _result(capsys)["ticks_replayed"] == 1


def test_errors_are_structured_without_raw_input(capsys: object) -> None:
    hostile = "missing-\x1b[2J-secret"
    assert main(["verify", hostile]) == 1
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert hostile not in captured.err
    error = json.loads(captured.err)
    assert error["status"] == "error"


def test_rebuild_repairs_closed_corrupt_projection(tmp_path: Path, capsys: object) -> None:
    run_dir = tmp_path / "run"
    _sealed_log(run_dir)
    connection = sqlite3.connect(run_dir / "state.sqlite3")
    try:
        connection.execute("UPDATE events SET hash = ? WHERE seq = 0", ("f" * 64,))
        connection.commit()
    finally:
        connection.close()

    assert main(["rebuild", str(run_dir)]) == 0
    assert _result(capsys)["sqlite"] == "ok"
    assert list((run_dir / "recovery").glob("state.sqlite3.pre-rebuild-*"))

    assert main(["verify", str(run_dir)]) == 0
    assert _result(capsys)["status"] == "ok"
