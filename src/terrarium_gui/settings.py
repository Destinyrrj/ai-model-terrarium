"""Runtime settings for the local-only GUI server."""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

_MIN_TOKEN_BYTES = 24
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{24,256}$")


def _session_token() -> SecretStr:
    # 32 random bytes gives a 256-bit, URL-safe session credential.
    return SecretStr(secrets.token_urlsafe(32))


class GuiSettings(BaseModel):
    """Immutable settings shared by the API, registry, and process manager.

    ``host`` is intentionally a literal.  Making the GUI remotely reachable is
    not a supported configuration because experiment configs and process
    controls are privileged local capabilities.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    runs_root: Path = Field(default_factory=lambda: Path.cwd() / "runs")
    configs_dir: Path = Field(default_factory=lambda: Path.cwd() / "configs")
    artifacts_root: Path = Field(
        default_factory=lambda: Path.cwd() / ".terrarium-gui-artifacts"
    )
    host: Literal["127.0.0.1"] = "127.0.0.1"
    bearer_token: SecretStr = Field(default_factory=_session_token, repr=False)
    dev_token: SecretStr | None = Field(default=None, repr=False)

    @field_validator("runs_root", "configs_dir", "artifacts_root", mode="after")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        # ``absolute`` normalizes the launch cwd without resolving symlinks.
        # Symlink policy is enforced by each filesystem owner before access.
        return value.absolute()

    @field_validator("bearer_token", mode="after")
    @classmethod
    def validate_session_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if _TOKEN_RE.fullmatch(token) is None:
            raise ValueError(
                f"access tokens must be {_MIN_TOKEN_BYTES}..256 URL-safe ASCII characters"
            )
        return value

    @field_validator("dev_token", mode="after")
    @classmethod
    def validate_development_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        token = value.get_secret_value()
        if _TOKEN_RE.fullmatch(token) is None:
            raise ValueError(
                f"development tokens must be {_MIN_TOKEN_BYTES}..256 URL-safe ASCII characters"
            )
        return value

    @model_validator(mode="after")
    def separate_owned_roots(self) -> GuiSettings:
        roots = {
            "runs_root": self.runs_root.resolve(strict=False),
            "configs_dir": self.configs_dir.resolve(strict=False),
            "artifacts_root": self.artifacts_root.resolve(strict=False),
        }
        items = list(roots.items())
        for index, (left_name, left) in enumerate(items):
            for right_name, right in items[index + 1 :]:
                if left == right or left in right.parents or right in left.parents:
                    raise ValueError(
                        f"{left_name} and {right_name} must be separate non-overlapping trees"
                    )
        return self

    @property
    def access_token(self) -> str:
        """Return the credential accepted by the server.

        A deliberately supplied development token replaces the random session
        token.  SecretStr fields keep either value out of repr/model dumps.
        """

        selected = self.dev_token or self.bearer_token
        return selected.get_secret_value()

    @property
    def token(self) -> str:
        """Short alias used by the CLI when printing the one-time launch URL."""

        return self.access_token

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Host header names accepted by the local HTTP application."""

        return frozenset({self.host, "localhost"})
