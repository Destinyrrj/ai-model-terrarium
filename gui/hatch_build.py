"""Build a self-contained GUI distribution without touching the sealed package."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Route workspace/sdist sources and require the compiled SPA in wheels."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            # A non-empty editable-specific map prevents Hatch from copying the
            # standard wheel's force-included package into site-packages, where
            # it would shadow ``dev-mode-dirs`` with a stale snapshot.
            build_data["force_include_editable"] = {
                "pyproject.toml": "_terrarium_gui_editable.marker"
            }
            return

        root = Path(self.root)
        workspace_source = root.parent / "src" / "terrarium_gui"
        sdist_source = root / "src" / "terrarium_gui"
        source = workspace_source if workspace_source.is_dir() else sdist_source

        if self.target_name == "sdist":
            if not workspace_source.is_dir():
                raise RuntimeError("workspace terrarium_gui source is missing")
            build_data.setdefault("force_include", {})[str(workspace_source)] = (
                "src/terrarium_gui"
            )
            license_file = root.parent / "LICENSE"
            if license_file.is_file():
                build_data["force_include"][str(license_file)] = "LICENSE"
            return

        if self.target_name == "wheel":
            if not source.is_dir():
                raise RuntimeError("terrarium_gui source is missing from build input")
            if not (source / "static" / "index.html").is_file():
                raise RuntimeError(
                    "frontend assets are missing; run `npm ci && npm run build` "
                    "in gui/frontend before building the wheel"
                )
            build_data.setdefault("force_include", {})[str(source)] = "terrarium_gui"
