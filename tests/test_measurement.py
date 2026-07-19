from __future__ import annotations

import json
from pathlib import Path

from terrarium.config import KnowledgeConfig
from terrarium.measurement import (
    LexicalKnowledgeClassifier,
    MeasurementBasis,
    Stance,
    authored_measurement_records,
    extract_inherited_exposures,
    extract_legacies,
    iter_committed_events,
    knowledge_survival_curve,
    write_measurements,
)


def _fact() -> KnowledgeConfig:
    return KnowledgeConfig(
        id="berries",
        statement="Red berries are poisonous after rain.",
        kind="true_rule",
        keywords=("red", "berries", "poisonous", "rain"),
    )


def test_lexical_classifier_reports_stance() -> None:
    classifier = LexicalKnowledgeClassifier()
    assert classifier.classify("red berries are poisonous after rain", _fact())[0] == Stance.ENTAILS
    assert (
        classifier.classify("red berries are not poisonous after rain", _fact())[0]
        == Stance.CONTRADICTS
    )
    assert classifier.classify("avoid the cave", _fact())[0] == Stance.SILENT


def test_curve_and_separate_outputs(tmp_path: Path) -> None:
    legacies = [
        {"generation": 0, "channel": "written", "text": "red berries poisonous rain"},
        {"generation": 0, "channel": "written", "text": "nothing useful"},
        {"generation": 1, "channel": "written", "text": "red berries poisonous rain"},
    ]
    points = knowledge_survival_curve(legacies, [_fact()], LexicalKnowledgeClassifier())
    assert [point.survival_rate for point in points] == [0.5, 1.0]
    assert {point.basis for point in points} == {MeasurementBasis.AUTHORED.value}
    output = tmp_path / "measurements"
    write_measurements(points, output)
    assert json.loads((output / "knowledge-survival.json").read_text())[0]["fact_id"] == "berries"
    assert (output / "knowledge-survival.csv").exists()


def test_extracts_actual_orchestrator_legacy_written_schema() -> None:
    events = [
        {
            "type": "legacy_written",
            "payload": {
                "legacy_id": "leg_123",
                "author_agent_id": "agent_123",
                "generation": 4,
                "channel": "written",
                "text": "red berries poisonous rain",
                "parent_legacy_ids": [],
            },
        }
    ]
    assert extract_legacies(events) == [
        {
            "id": "leg_123",
            "generation": 4,
            "channel": "written",
            "text": "red berries poisonous rain",
        }
    ]


def test_inherited_exposures_measure_final_live_cohort_and_zero_coverage() -> None:
    events = [
        {
            "type": "agent_spawned",
            "payload": {
                "agent_id": "agent_0",
                "lineage_id": "lineage_0",
                "generation": 0,
                "inherited_legacy_ids": [],
            },
        },
        {
            "type": "legacy_written",
            "payload": {
                "legacy_id": "leg_0",
                "generation": 0,
                "channel": "written",
                "text": "red berries poisonous rain",
            },
        },
        {
            "type": "agent_spawned",
            "payload": {
                "agent_id": "agent_1",
                "lineage_id": "lineage_0",
                "generation": 1,
                "inherited_legacy_ids": ["leg_0"],
            },
        },
        {
            "type": "legacy_written",
            "payload": {
                "legacy_id": "leg_1",
                "generation": 1,
                "channel": "written",
                "text": "red berries poisonous rain",
            },
        },
        {
            "type": "agent_spawned",
            "payload": {
                "agent_id": "agent_2",
                "lineage_id": "lineage_0",
                "generation": 2,
                "inherited_legacy_ids": ["leg_1"],
            },
        },
    ]
    legacies = extract_legacies(events)
    inherited = extract_inherited_exposures(events, legacies)

    assert [(record["generation"], record["legacy_id"]) for record in inherited] == [
        (1, "leg_0"),
        (2, "leg_1"),
    ]
    assert {record["basis"] for record in inherited} == {MeasurementBasis.INHERITED.value}

    records = [*authored_measurement_records(legacies), *inherited]
    points = knowledge_survival_curve(
        records,
        [_fact()],
        LexicalKnowledgeClassifier(),
        expected_generations=range(3),
        expected_channels=("written",),
        expected_bases=tuple(MeasurementBasis),
    )
    assert len(points) == 6
    indexed = {(point.basis, point.generation): point for point in points}
    assert indexed[("authored", 2)].total_legacies == 0
    assert indexed[("inherited", 0)].total_legacies == 0
    assert indexed[("inherited", 2)].entails == 1


def test_measurement_outputs_replace_symlink_without_following_it(tmp_path: Path) -> None:
    output = tmp_path / "measurements"
    output.mkdir()
    target = tmp_path / "must-not-change.txt"
    target.write_text("sealed", encoding="utf-8")
    (output / "knowledge-survival.json").symlink_to(target)
    (output / "knowledge-survival.csv").symlink_to(target)

    write_measurements([], output)

    assert target.read_text(encoding="utf-8") == "sealed"
    assert not (output / "knowledge-survival.json").is_symlink()
    assert not (output / "knowledge-survival.csv").is_symlink()
    assert json.loads((output / "knowledge-survival.json").read_text()) == []


def test_incomplete_final_event_tail_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    begin = {"tick": 0, "type": "tick_begin", "payload": {}}
    good = {
        "tick": 0,
        "type": "legacy_created",
        "payload": {
            "id": "x",
            "generation": 0,
            "channel": "written",
            "text": "claim",
        },
    }
    commit = {"tick": 0, "type": "tick_commit", "payload": {}}
    path.write_bytes(
        b"\n".join(json.dumps(item).encode() for item in (begin, good, commit))
        + b"\n"
        + b'{"type":'
    )
    events = list(iter_committed_events(path))
    assert extract_legacies(events)[0]["id"] == "x"


def test_fully_written_uncommitted_tick_is_not_measured(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    records = [
        {"tick": 1, "type": "tick_begin", "payload": {}},
        {
            "tick": 1,
            "type": "legacy_created",
            "payload": {
                "id": "not-durable",
                "generation": 1,
                "channel": "written",
                "text": "claim",
            },
        },
    ]
    path.write_bytes(b"\n".join(json.dumps(item).encode() for item in records) + b"\n")
    assert list(iter_committed_events(path)) == []
