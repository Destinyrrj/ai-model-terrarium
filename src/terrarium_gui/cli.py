"""Command line entry point for the loopback-only Terrarium GUI server."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

import uvicorn
from pydantic import SecretStr, ValidationError

from .app import create_app
from .settings import GuiSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terrarium-gui", description="Serve the local AI Model Terrarium GUI."
    )
    parser.add_argument("command", nargs="?", choices=("serve",), default="serve")
    parser.add_argument("--port", type=_port, default=8765)
    parser.add_argument("--runs-root", type=Path, default=Path("runs"))
    parser.add_argument("--configs-dir", type=Path, default=Path("configs"))
    parser.add_argument("--artifacts-root", type=Path, default=Path(".terrarium-gui-artifacts"))
    parser.add_argument(
        "--dev-token-file",
        type=Path,
        help="0600 file containing a fixed development bearer token",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = GuiSettings(
            runs_root=args.runs_root,
            configs_dir=args.configs_dir,
            artifacts_root=args.artifacts_root,
            dev_token=(
                SecretStr(_read_token_file(args.dev_token_file))
                if args.dev_token_file is not None
                else None
            ),
        )
        app = create_app(settings)
    except (OSError, ValueError, ValidationError) as exc:
        sys.stderr.write(f"terrarium-gui: startup failed ({type(exc).__name__})\n")
        return 2

    # URL fragments are not transmitted in HTTP requests, keeping the bootstrap
    # credential out of access logs and browser referrers.
    encoded_token = quote(settings.token, safe="")
    sys.stdout.write(f"Terrarium GUI: http://127.0.0.1:{args.port}/#token={encoded_token}\n")
    sys.stdout.flush()
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        server_header=False,
    )
    return 0


def _port(value: str) -> int:
    try:
        port = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be in 1..65535")
    return port


def _read_token_file(path: Path) -> str:
    flags = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0)) | int(
        getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > 512
        ):
            raise ValueError("development token file must be a private 0600 regular file")
        data = os.read(fd, 513)
    finally:
        os.close(fd)
    try:
        return data.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("development token file must contain ASCII") from exc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
