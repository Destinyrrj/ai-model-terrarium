#!/usr/bin/env bash
#
# Seal a Claude Code run config against this host.
#
# A `claude-code` run config pins the absolute path to the installed `claude`
# binary and that exact binary's sha256 (src/terrarium/factory.py verifies the
# digest and fails the run on drift). Those two values are host-specific and must
# never be committed. This helper resolves the binary, renders the committed
# template with the resolved path + digest, schema-validates the result, and
# verifies the digest matches -- producing a gitignored configs/*.local.yaml
# ready for `terrarium run`. It performs no network I/O and starts no model.
#
# Usage:
#   scripts/seal-claude-config.sh [--template PATH] [--output PATH] [--claude PATH]
#
# Defaults:
#   --template  configs/pilot-claude.yaml.template
#   --output    configs/pilot-claude.local.yaml
#   --claude    $(command -v claude)

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

TEMPLATE="${REPO_ROOT}/configs/pilot-claude.yaml.template"
OUTPUT="${REPO_ROOT}/configs/pilot-claude.local.yaml"
CLAUDE_BIN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --template) TEMPLATE="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --claude) CLAUDE_BIN="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "error: unknown argument '$1'" >&2
      exit 2 ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  echo "error: required command 'uv' was not found" >&2
  exit 127
fi

if [[ -z "${CLAUDE_BIN}" ]]; then
  if ! command -v claude >/dev/null 2>&1; then
    echo "error: 'claude' was not found on PATH; install Claude Code or pass --claude PATH" >&2
    exit 127
  fi
  CLAUDE_BIN="$(command -v claude)"
fi

if [[ ! -f "${TEMPLATE}" ]]; then
  echo "error: template not found: ${TEMPLATE}" >&2
  exit 2
fi

mkdir -p "${REPO_ROOT}/runs" "${REPO_ROOT}/configs"

# Render + validate + verify in one offline step, reusing the sealed simulation's
# own loader and factory so the sealed config is guaranteed runnable (modulo the
# runtime model auth/network, which this step intentionally does not touch).
UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}" \
  uv run --project "${REPO_ROOT}" python - "${TEMPLATE}" "${OUTPUT}" "${CLAUDE_BIN}" <<'PY'
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from terrarium.config import load_config
from terrarium.factory import build_adapter_factory

template_path, output_path, claude_hint = sys.argv[1], sys.argv[2], sys.argv[3]

resolved = Path(claude_hint).resolve(strict=True)
if not resolved.is_file():
    sys.exit(f"error: resolved claude path is not a regular file: {resolved}")

# Guard against paths that cannot be embedded safely in a double-quoted YAML
# scalar. Such paths are pathological; ask the operator to seal by hand instead.
if any(ord(ch) < 0x20 for ch in str(resolved)) or '"' in str(resolved) or "\\" in str(resolved):
    sys.exit(f"error: claude path contains characters unsafe for YAML: {resolved!r}")

digest = hashlib.sha256(resolved.read_bytes()).hexdigest()

text = Path(template_path).read_text(encoding="utf-8")
for token in ("__CLAUDE_EXECUTABLE__", "__CLAUDE_SHA256__"):
    if token not in text:
        sys.exit(f"error: template is missing placeholder {token}")

rendered = text.replace("__CLAUDE_EXECUTABLE__", str(resolved)).replace(
    "__CLAUDE_SHA256__", digest
)

out = Path(output_path)
out.write_text(rendered, encoding="utf-8")

# Schema-validate, then resolve + verify the pinned digest against the binary.
config = load_config(out)
build_adapter_factory(config)

print(
    json.dumps(
        {
            "status": "sealed",
            "config": str(out),
            "executable": str(resolved),
            "executable_sha256": digest,
            "run_id": config.run_id,
        },
        indent=2,
        sort_keys=True,
    )
)
PY

echo
echo "Sealed config written and verified. Next steps:"
echo "  1. Seed the tokenizer cache (offline determinism):"
echo "       export TIKTOKEN_CACHE_DIR=\"${REPO_ROOT}/.tiktoken-cache\""
echo "       uv run python -c \"import tiktoken; tiktoken.get_encoding('cl100k_base')\""
echo "  2. Run the pilot:"
echo "       uv run terrarium run '${OUTPUT}' --output runs/pilot-claude"
echo "See docs/claude-code.md (\"First real-model run\") for the full runbook."
