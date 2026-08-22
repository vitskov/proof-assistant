#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_path="${REPOPROVER_CODEX_VENV:-${HOME}/.venvs/repoprover-codex}"
cache_home="${REPOPROVER_CODEX_CACHE_HOME:-${HOME}/.cache/repoprover-codex}"
python_spec="${REPOPROVER_CODEX_PYTHON:-3.13}"

case "${venv_path}" in
  *[Dd][Rr][Oo][Pp][Bb][Oo][Xx]*)
    echo "ERROR: Python environments must not reside in Dropbox: ${venv_path}" >&2
    exit 2
    ;;
esac

case "${cache_home}" in
  *[Dd][Rr][Oo][Pp][Bb][Oo][Xx]*)
    echo "ERROR: Package caches must not reside in Dropbox: ${cache_home}" >&2
    exit 2
    ;;
esac

case "${cache_home}" in
  "${HOME}"/*) ;;
  *)
    echo "ERROR: Package caches must reside inside the user home: ${cache_home}" >&2
    exit 2
    ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv is required for this development install" >&2
  exit 2
fi

mkdir -p "$(dirname "${venv_path}")"
if [[ ! -x "${venv_path}/bin/python" ]]; then
  uv venv --python "${python_spec}" "${venv_path}"
fi

uv pip install --python "${venv_path}/bin/python" -e "${project_root}[dev]"

# Installation is not considered successful merely because a compiler exists.
# This command compiles and executes a C program, and detects incompatible Lean
# bundled compilers before Lake starts expensive dependency work.
"${venv_path}/bin/repoprover-codex" compiler-check
"${venv_path}/bin/repoprover-codex" cache init
"${venv_path}/bin/python" -m pytest -q "${project_root}/tests"

echo "Development environment ready: ${venv_path}"
