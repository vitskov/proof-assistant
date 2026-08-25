#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_path="${PROOF_ASSISTANT_VENV:-${REPOPROVER_CODEX_VENV:-${HOME}/.venvs/proof-assistant}}"
# Keep the established cache path. Changing it would duplicate the large
# shared Mathlib/dependency depot on existing installations.
cache_home="${PROOF_ASSISTANT_CACHE_HOME:-${REPOPROVER_CODEX_CACHE_HOME:-${HOME}/.cache/repoprover-codex}}"
python_spec="${PROOF_ASSISTANT_PYTHON:-${REPOPROVER_CODEX_PYTHON:-3.13}}"
uv_install_dir="${PROOF_ASSISTANT_UV_INSTALL_DIR:-${HOME}/.local/bin}"

# Hardware/OS floor for local Lean/Mathlib builds. Override only for a site
# policy that has verified its own hardware; do not lower these to work
# around a genuinely underpowered machine.
min_cpu_cores="${PROOF_ASSISTANT_MIN_CPU_CORES:-4}"
min_memory_gib="${PROOF_ASSISTANT_MIN_MEMORY_GIB:-16}"
recommended_cpu_cores=8
recommended_memory_gib=32

os_name="$(uname -s)"
os_release="$(uname -r)"
cpu_cores=0
memory_bytes=0

case "${os_name}" in
  Darwin)
    # Darwin 21.x is macOS 12 Monterey, the oldest release this project
    # supports on both Intel and Apple Silicon Macs.
    darwin_major="${os_release%%.*}"
    if [[ "${darwin_major}" -lt 21 ]]; then
      echo "ERROR: macOS 12 Monterey (Darwin 21) or newer is required; detected Darwin ${os_release}" >&2
      exit 2
    fi
    cpu_cores="$(sysctl -n hw.physicalcpu 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 0)"
    memory_bytes="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
    ;;
  Linux)
    glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
    if [[ -z "${glibc_version}" ]]; then
      echo "ERROR: Unable to determine glibc version; glibc 2.31 (Ubuntu 20.04-equivalent) or newer is required." >&2
      exit 2
    fi
    glibc_major="${glibc_version%%.*}"
    glibc_minor="${glibc_version#*.}"
    glibc_minor="${glibc_minor%%.*}"
    if [[ "${glibc_major}" -lt 2 ]] \
      || { [[ "${glibc_major}" -eq 2 ]] && [[ "${glibc_minor}" -lt 31 ]]; }; then
      echo "ERROR: glibc 2.31 (Ubuntu 20.04-equivalent) or newer is required; detected glibc ${glibc_version}" >&2
      exit 2
    fi
    if ! command -v lscpu >/dev/null 2>&1; then
      echo "ERROR: Cannot determine physical CPU cores because lscpu is unavailable." >&2
      exit 2
    fi
    cpu_cores="$(lscpu -p=socket,core 2>/dev/null | awk -F, \
      '!/^#/ && NF >= 2 { cores[$1 ":" $2] = 1 } END { print length(cores) + 0 }')"
    memory_bytes="$(awk '/^MemTotal:/ { print $2 * 1024 }' /proc/meminfo 2>/dev/null || echo 0)"
    ;;
  *)
    echo "ERROR: Unsupported operating system: ${os_name}. Proof Assistant supports macOS and Linux only." >&2
    exit 2
    ;;
esac

cpu_cores="${cpu_cores:-0}"
memory_bytes="${memory_bytes:-0}"

if [[ "${cpu_cores}" -lt "${min_cpu_cores}" ]]; then
  echo "ERROR: at least ${min_cpu_cores} CPU cores are required; detected ${cpu_cores}" >&2
  exit 2
fi

memory_gib=$(( memory_bytes / 1024 / 1024 / 1024 ))
if [[ "${memory_gib}" -lt "${min_memory_gib}" ]]; then
  echo "ERROR: at least ${min_memory_gib} GiB of RAM is required; detected ${memory_gib} GiB" >&2
  exit 2
fi

echo "System check: ${os_name} ${os_release}, ${cpu_cores} CPU cores, ${memory_gib} GiB RAM"
if [[ "${cpu_cores}" -lt "${recommended_cpu_cores}" ]] \
  || [[ "${memory_gib}" -lt "${recommended_memory_gib}" ]]; then
  echo "NOTE: ${recommended_cpu_cores}+ CPU cores and ${recommended_memory_gib}+ GiB RAM are recommended for faster Lean/Mathlib builds." >&2
fi

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

configure_shell_path() {
  local shell_name="${SHELL:-sh}"
  shell_name="${shell_name##*/}"
  local path_dir="${venv_path}/bin"
  local quoted_path config path_line
  local configs=()

  # Use shell-specific startup files. Login shells and interactive shells can
  # read different files, so configure both where that distinction exists.
  printf -v quoted_path '%q' "${path_dir}"
  case "${shell_name}" in
    zsh)
      configs=("${HOME}/.zprofile" "${HOME}/.zshrc")
      path_line="case \":\$PATH:\" in *\":${quoted_path}:\"*) ;; *) export PATH=${quoted_path}:\"\$PATH\";; esac"
      ;;
    bash)
      configs=("${HOME}/.bash_profile" "${HOME}/.bashrc")
      path_line="case \":\$PATH:\" in *\":${quoted_path}:\"*) ;; *) export PATH=${quoted_path}:\"\$PATH\";; esac"
      ;;
    fish)
      config="${HOME}/.config/fish/config.fish"
      configs=("${config}")
      printf -v quoted_path '%q' "${path_dir}"
      path_line="fish_add_path --path ${quoted_path}"
      ;;
    *)
      configs=("${HOME}/.profile")
      path_line="case \":\$PATH:\" in *\":${quoted_path}:\"*) ;; *) export PATH=${quoted_path}:\"\$PATH\";; esac"
      ;;
  esac

  for config in "${configs[@]}"; do
    mkdir -p "$(dirname "${config}")"
    if ! grep -Fqx "${path_line}" "${config}" 2>/dev/null; then
      printf '\n# Added by Proof Assistant installer\n%s\n' "${path_line}" >> "${config}"
    fi
  done
  echo "Added proof-assistant to ${shell_name} startup path (${configs[*]})"
}

configure_shell_path
echo "Development environment ready: ${venv_path}"
