from __future__ import annotations

from pathlib import Path

import pytest

from terrarium_gui.cli import _read_token_file, build_parser


def test_development_token_uses_private_file_not_argv_secret(tmp_path: Path) -> None:
    parser = build_parser()
    options = {option for action in parser._actions for option in action.option_strings}
    assert "--dev-token" not in options
    assert "--dev-token-file" in options

    token_file = tmp_path / "token"
    token_file.write_text("a" * 32 + "\n", encoding="ascii")
    token_file.chmod(0o600)
    assert _read_token_file(token_file) == "a" * 32

    token_file.chmod(0o644)
    with pytest.raises(ValueError, match="private 0600"):
        _read_token_file(token_file)
