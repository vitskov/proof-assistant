#!/usr/bin/env bash
set -euo pipefail

readonly project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly task_dir="$(mktemp -d)"
trap 'rm -rf "${task_dir}"' EXIT

export UV_NO_MODIFY_PATH=1
uv_bin="$(${project_root}/scripts/bootstrap-uv.sh "${task_dir}/uv")"
readonly uv_bin

if [[ "$("${uv_bin}" --version)" != "uv 0.12.0"* ]]; then
  echo "ERROR: lock generation requires the repository-pinned uv 0.12.0" >&2
  exit 2
fi

cd "${project_root}"
"${uv_bin}" pip compile requirements/py313-build.txt \
  --python-version 3.13 \
  --universal \
  --generate-hashes \
  --no-header \
  -o requirements/py313-build.lock
"${uv_bin}" pip compile pyproject.toml requirements/py313-build.txt \
  --extra dev \
  --python-version 3.13 \
  --universal \
  --generate-hashes \
  --no-header \
  -o requirements/py313-dev.lock
