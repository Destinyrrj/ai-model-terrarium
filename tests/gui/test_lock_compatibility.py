from __future__ import annotations

import tomllib
from pathlib import Path

MANIFEST_DEPENDENCIES = frozenset({"numpy", "pydantic", "pyyaml", "tiktoken"})


def _locked_versions(path: Path) -> dict[str, str]:
    with path.open("rb") as stream:
        lock = tomllib.load(stream)
    versions: dict[str, str] = {}
    for package in lock["package"]:
        name = package.get("name")
        version = package.get("version")
        if name in MANIFEST_DEPENDENCIES and isinstance(version, str):
            versions[name] = version
    return versions


def test_gui_lock_matches_manifest_critical_root_versions() -> None:
    root = Path(__file__).resolve().parents[2]
    root_versions = _locked_versions(root / "uv.lock")
    gui_versions = _locked_versions(root / "gui" / "uv.lock")

    assert root_versions.keys() == MANIFEST_DEPENDENCIES
    assert gui_versions == root_versions
