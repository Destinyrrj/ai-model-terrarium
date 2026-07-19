from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from terrarium.config import load_config
from terrarium.replay import mechanics_config


def test_mvp_config_is_strict_and_stable() -> None:
    config = load_config(Path("configs/mvp.yaml"))
    assert config.run_id == "mvp-local"
    assert config.runtime.adapter == "mock"
    assert config.world.starvation_lethal is False
    assert config.world.collapse_lethal is False
    mechanics = mechanics_config(config)
    assert mechanics.starvation_lethal is False
    assert mechanics.collapse_lethal is False
    assert config.digest() == config.digest()
    assert len(config.digest()) == 64


@pytest.mark.parametrize(
    "bad_text",
    [
        "schema_version: 1\nschema_version: 1\n",
        "!!python/object/apply:os.system ['id']\n",
        "- not\n- a\n- mapping\n",
    ],
)
def test_unsafe_or_ambiguous_yaml_is_rejected(tmp_path: Path, bad_text: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(bad_text, encoding="utf-8")
    with pytest.raises((ValueError, yaml.YAMLError)):
        load_config(path)


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    original = Path("configs/mvp.yaml").read_text(encoding="utf-8")
    path = tmp_path / "unknown.yaml"
    path.write_text(original + "host_shell: true\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="host_shell"):
        load_config(path)


def test_process_backend_needs_explicit_acknowledgement(tmp_path: Path) -> None:
    original = Path("configs/mvp.yaml").read_text(encoding="utf-8")
    changed = original.replace("adapter: mock", "adapter: subprocess")
    changed = changed.replace(
        "timeout_seconds: 60",
        "argv: [/usr/bin/false]\n  executable_sha256: " + "0" * 64 + "\n  timeout_seconds: 60",
    )
    changed = changed.replace("backend: mock", "backend: process")
    path = tmp_path / "unsafe.yaml"
    path.write_text(changed, encoding="utf-8")
    with pytest.raises(ValidationError, match="acknowledgement"):
        load_config(path)


def test_claude_code_requires_single_pinned_executable_and_host_boundary() -> None:
    config = load_config("configs/mvp.yaml")
    runtime = config.runtime.model_copy(
        update={
            "adapter": "claude-code",
            "argv": ("/usr/bin/claude",),
            "executable_sha256": "0" * 64,
            "sandbox": config.runtime.sandbox.model_copy(
                update={
                    "backend": "process",
                    "network": "inherit",
                    "acknowledge_unsafe_host_execution": True,
                }
            ),
        }
    )
    validated = type(runtime).model_validate(runtime.model_dump(mode="python"))
    assert validated.adapter == "claude-code"

    with pytest.raises(ValidationError, match="only the executable"):
        type(runtime).model_validate(
            {**runtime.model_dump(mode="python"), "argv": ["claude", "--bare"]}
        )


def test_proxy_label_needs_external_enforcement_attestation(tmp_path: Path) -> None:
    original = Path("configs/mvp.yaml").read_text(encoding="utf-8")
    changed = original.replace("network: none", "network: provider-proxy")
    changed = changed.replace(
        "acknowledge_unsafe_host_execution: false",
        "egress_proxy: http://broker.invalid:8080\n    acknowledge_unsafe_host_execution: false",
    )
    path = tmp_path / "unforced-proxy.yaml"
    path.write_text(changed, encoding="utf-8")
    with pytest.raises(ValidationError, match="externally enforced"):
        load_config(path)


@pytest.mark.parametrize(
    "proxy",
    [
        "https://user:secret@broker.invalid:8443",
        "https://broker.invalid:8443/?token=secret",
        "https://broker.invalid:8443/#secret",
        "https://broker.invalid:8443/secret",
    ],
)
def test_proxy_url_cannot_smuggle_credentials_into_manifest(tmp_path: Path, proxy: str) -> None:
    original = Path("configs/mvp.yaml").read_text(encoding="utf-8")
    changed = original.replace("network: none", "network: provider-proxy")
    changed = changed.replace(
        "acknowledge_unsafe_host_execution: false",
        f"egress_proxy: {proxy}\n"
        "    external_egress_enforced: true\n"
        "    acknowledge_unsafe_host_execution: false",
    )
    path = tmp_path / "secret-proxy.yaml"
    path.write_text(changed, encoding="utf-8")
    with pytest.raises(ValidationError, match="egress_proxy"):
        load_config(path)


def test_config_reader_rejects_symlinks_and_special_files(tmp_path: Path) -> None:
    source = tmp_path / "source.yaml"
    source.write_bytes(Path("configs/mvp.yaml").read_bytes())
    linked = tmp_path / "linked.yaml"
    linked.symlink_to(source)
    with pytest.raises(OSError):
        load_config(linked)

    fifo = tmp_path / "config.fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        load_config(fifo)


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        ("starvation_damage: 15", "starvation_damage: 0"),
        ("poison_damage: 40", "poison_damage: 0"),
        ("max_stderr_bytes: 16384", "max_stderr_bytes: 0"),
    ],
)
def test_config_bounds_match_runtime_mechanics(
    tmp_path: Path, needle: str, replacement: str
) -> None:
    original = Path("configs/mvp.yaml").read_text(encoding="utf-8")
    path = tmp_path / "mismatch.yaml"
    path.write_text(original.replace(needle, replacement), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)
