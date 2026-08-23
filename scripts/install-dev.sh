#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_path="${PROOF_ASSISTANT_VENV:-${REPOPROVER_CODEX_VENV:-${HOME}/.venvs/proof-assistant}}"
# Keep the established cache path. Changing it would duplicate the large
# shared Mathlib/dependency depot on existing installations.
cache_home="${PROOF_ASSISTANT_CACHE_HOME:-${REPOPROVER_CODEX_CACHE_HOME:-${HOME}/.cache/repoprover-codex}}"
python_spec="${PROOF_ASSISTANT_PYTHON:-${REPOPROVER_CODEX_PYTHON:-3.13}}"

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
"${venv_path}/bin/proof-assistant" compiler-check
"${venv_path}/bin/proof-assistant" cache init
"${venv_path}/bin/python" -m pytest -q "${project_root}/tests"

echo "Development environment ready: ${venv_path}"
