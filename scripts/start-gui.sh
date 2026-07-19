#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
GUI_ROOT="${REPO_ROOT}/gui"
FRONTEND_ROOT="${GUI_ROOT}/frontend"
STATIC_INDEX="${REPO_ROOT}/src/terrarium_gui/static/index.html"

for command_name in uv npm; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "error: required command '${command_name}' was not found" >&2
    exit 127
  fi
done

export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/terrarium-uv-cache}"

mkdir -p "${REPO_ROOT}/runs" "${REPO_ROOT}/configs"

echo "[terrarium-gui] Synchronizing locked Python dependencies..."
uv sync --project "${GUI_ROOT}" --frozen

if [[ ! -f "${FRONTEND_ROOT}/node_modules/.package-lock.json" || \
      "${FRONTEND_ROOT}/package-lock.json" -nt "${FRONTEND_ROOT}/node_modules/.package-lock.json" ]]; then
  echo "[terrarium-gui] Installing locked frontend dependencies..."
  npm --prefix "${FRONTEND_ROOT}" ci
fi

needs_frontend_build=false
if [[ ! -f "${STATIC_INDEX}" ]]; then
  needs_frontend_build=true
elif [[ "${FRONTEND_ROOT}/package.json" -nt "${STATIC_INDEX}" || \
        "${FRONTEND_ROOT}/package-lock.json" -nt "${STATIC_INDEX}" || \
        "${FRONTEND_ROOT}/index.html" -nt "${STATIC_INDEX}" || \
        "${FRONTEND_ROOT}/tsconfig.json" -nt "${STATIC_INDEX}" || \
        "${FRONTEND_ROOT}/tsconfig.app.json" -nt "${STATIC_INDEX}" || \
        "${FRONTEND_ROOT}/tsconfig.node.json" -nt "${STATIC_INDEX}" || \
        "${FRONTEND_ROOT}/vite.config.ts" -nt "${STATIC_INDEX}" ]]; then
  needs_frontend_build=true
elif [[ -n "$(find "${FRONTEND_ROOT}/src" -type f -newer "${STATIC_INDEX}" -print -quit)" ]]; then
  needs_frontend_build=true
fi

if [[ "${needs_frontend_build}" == true ]]; then
  echo "[terrarium-gui] Building the frontend..."
  npm --prefix "${FRONTEND_ROOT}" run build
fi

echo "[terrarium-gui] Starting the loopback server..."
cd -- "${REPO_ROOT}"
exec uv run --project "${GUI_ROOT}" --frozen terrarium-gui serve \
  --runs-root "${REPO_ROOT}/runs" \
  --configs-dir "${REPO_ROOT}/configs" \
  "$@"
