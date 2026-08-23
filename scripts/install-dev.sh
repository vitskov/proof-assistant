#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_path="${PROOF_ASSISTANT_VENV:-${REPOPROVER_CODEX_VENV:-${HOME}/.venvs/proof-assistant}}"
# Keep the established cache path. Changing it would duplicate the large
# shared Mathlib/dependency depot on existing installations.
cache_home="${PROOF_ASSISTANT_CACHE_HOME:-${REPOPROVER_CODEX_CACHE_HOME:-${HOME}/.cache/repoprover-codex}}"
python_spec="${PROOF_ASSISTANT_PYTHON:-${REPOPROVER_CODEX_PYTHON:-3.13}}"
uv_install_dir="${PROOF_ASSISTANT_UV_INSTALL_DIR:-${HOME}/.local/bin}"

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

uv_bin=""
uv_candidate="$(command -v uv 2>/dev/null || true)"
if [[ -n "${uv_candidate}" ]] && "${uv_candidate}" --version >/dev/null 2>&1; then
  uv_bin="${uv_candidate}"
elif [[ "${uv_candidate}" != "${uv_install_dir}/uv" ]] \
  && [[ -x "${uv_install_dir}/uv" ]] \
  && "${uv_install_dir}/uv" --version >/dev/null 2>&1; then
  uv_bin="${uv_install_dir}/uv"
fi
if [[ -n "${uv_bin}" ]]; then
  if [[ "${uv_bin}" == "${uv_install_dir}/uv" ]]; then
    PATH="${uv_install_dir}:${PATH}"
    export PATH
  fi
  echo "Using uv: ${uv_bin}"
else
  if [[ -n "${uv_candidate}" ]]; then
    echo "Existing uv is not working; bootstrapping the official Astral standalone installer." >&2
  else
    echo "uv was not found; bootstrapping the official Astral standalone installer." >&2
  fi

  if ! mkdir -p "${uv_install_dir}"; then
    echo "ERROR: Cannot create the uv install directory: ${uv_install_dir}" >&2
    exit 2
  fi
  if command -v curl >/dev/null 2>&1; then
    if ! curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR="${uv_install_dir}" UV_NO_MODIFY_PATH=1 sh; then
      echo "ERROR: Failed to download or run Astral's uv installer with curl." >&2
      exit 2
    fi
  elif command -v wget >/dev/null 2>&1; then
    if ! wget -qO- https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR="${uv_install_dir}" UV_NO_MODIFY_PATH=1 sh; then
      echo "ERROR: Failed to download or run Astral's uv installer with wget." >&2
      exit 2
    fi
  else
    echo "ERROR: Cannot bootstrap uv because neither curl nor wget is installed." >&2
    exit 2
  fi

  # This export changes only this installer process and its children. The
  # official installer is explicitly forbidden from editing shell profiles.
  PATH="${uv_install_dir}:${PATH}"
  export PATH
  uv_bin="$(command -v uv 2>/dev/null || true)"
  if [[ -z "${uv_bin}" ]] || [[ ! -x "${uv_bin}" ]] \
    || ! "${uv_bin}" --version >/dev/null 2>&1; then
    echo "ERROR: Astral's installer did not produce a working uv in ${uv_install_dir}" >&2
    exit 2
  fi
  echo "Bootstrapped uv: ${uv_bin}"
fi

# Resolve once after discovery/bootstrap and use this exact executable for all
# remaining uv operations.
uv_bin="$(command -v uv)"

mkdir -p "$(dirname "${venv_path}")"
if [[ ! -x "${venv_path}/bin/python" ]]; then
  "${uv_bin}" venv --python "${python_spec}" "${venv_path}"
fi

"${uv_bin}" pip install --python "${venv_path}/bin/python" -e "${project_root}[dev]"

# Installation is not considered successful merely because a compiler exists.
# This command compiles and executes a C program, and detects incompatible Lean
# bundled compilers before Lake starts expensive dependency work.
"${venv_path}/bin/proof-assistant" compiler-check
"${venv_path}/bin/proof-assistant" cache init
"${venv_path}/bin/python" -m pytest -q "${project_root}/tests"

echo "Development environment ready: ${venv_path}"
