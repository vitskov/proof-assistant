#!/usr/bin/env bash
set -euo pipefail

repository_url="${PROOF_ASSISTANT_REPOSITORY_URL:-https://github.com/vitskov/proof-assistant.git}"
install_ref="${PROOF_ASSISTANT_REF:-main}"
source_dir="${PROOF_ASSISTANT_SOURCE_DIR:-${HOME}/.local/share/proof-assistant/source}"
repoprover_url="${PROOF_ASSISTANT_REPOPROVER_URL:-https://github.com/facebookresearch/repoprover.git}"
repoprover_ref="${PROOF_ASSISTANT_REPOPROVER_REF:-386adba3df572cb71df534add2c764e071898a2e}"
elan_home="${PROOF_ASSISTANT_ELAN_HOME:-${ELAN_HOME:-${HOME}/.elan}}"
repoprover_source_was_explicit=0
if [[ -n "${PROOF_ASSISTANT_REPOPROVER_SOURCE:-}" ]]; then
  repoprover_source="${PROOF_ASSISTANT_REPOPROVER_SOURCE}"
  repoprover_source_was_explicit=1
else
  repoprover_source="${HOME}/.local/share/proof-assistant/repoprover"
fi

# When executed from a checkout, install that checkout. When streamed to Bash,
# the script has no repository path and bootstraps the managed source checkout
# after the platform and hardware checks below.
project_root=""
installer_path="${BASH_SOURCE[0]:-}"
if [[ -n "${installer_path}" && -f "${installer_path}" ]]; then
  candidate_root="$(cd "$(dirname "${installer_path}")" && pwd)"
  if [[ -f "${candidate_root}/pyproject.toml" \
    && -d "${candidate_root}/src/proof_assistant" ]]; then
    project_root="${candidate_root}"
  fi
fi

venv_path="${PROOF_ASSISTANT_VENV:-${REPOPROVER_CODEX_VENV:-${HOME}/.venvs/proof-assistant}}"
# Keep the established cache path. Changing it would duplicate the large
# shared Mathlib/dependency depot on existing installations.
cache_home="${PROOF_ASSISTANT_CACHE_HOME:-${REPOPROVER_CODEX_CACHE_HOME:-${HOME}/.cache/repoprover-codex}}"
python_spec="${PROOF_ASSISTANT_PYTHON:-${REPOPROVER_CODEX_PYTHON:-3.13}}"
uv_home="${PROOF_ASSISTANT_UV_HOME:-${PROOF_ASSISTANT_UV_INSTALL_DIR:-${HOME}/.local/share/proof-assistant/uv}}"

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
    if command -v lscpu >/dev/null 2>&1; then
      cpu_cores="$(lscpu -p=socket,core 2>/dev/null | awk -F, \
        '!/^#/ && NF >= 2 { cores[$1 ":" $2] = 1 } END { print length(cores) + 0 }')"
    else
      cpu_cores="$(awk '
        /^physical id[[:space:]]*:/ { socket = $NF }
        /^core id[[:space:]]*:/ { cores[socket ":" $NF] = 1 }
        /^processor[[:space:]]*:/ { logical += 1 }
        END {
          physical = length(cores)
          print (physical > 0 ? physical : logical + 0)
        }
      ' /proc/cpuinfo 2>/dev/null || echo 0)"
    fi
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

canonicalize_directory_target() {
  local path="$1"
  local label="$2"
  local probe component suffix="" resolved old_ifs
  local -a path_components=()

  case "${path}" in
    /*) ;;
    *)
      echo "ERROR: ${label} must be an absolute path: ${path}" >&2
      return 2
      ;;
  esac

  probe="${path}"
  while [[ ! -e "${probe}" && ! -L "${probe}" ]]; do
    component="${probe##*/}"
    suffix="/${component}${suffix}"
    probe="${probe%/*}"
    if [[ -z "${probe}" ]]; then
      probe="/"
    fi
  done
  if [[ ! -d "${probe}" ]]; then
    echo "ERROR: ${label} has a non-directory path component: ${probe}" >&2
    return 2
  fi

  resolved="$(cd -P "${probe}" && pwd)"
  # Bash 3.2 on macOS treats expansion of an empty array as an unbound
  # variable under `set -u`, so only create and iterate components when a
  # non-existing suffix actually needs normalization.
  if [[ -n "${suffix}" ]]; then
    old_ifs="${IFS}"
    IFS='/'
    read -r -a path_components <<< "${suffix#/}"
    IFS="${old_ifs}"
    for component in "${path_components[@]}"; do
      case "${component}" in
        ""|.) ;;
        ..)
          if [[ "${resolved}" != "/" ]]; then
            resolved="${resolved%/*}"
            if [[ -z "${resolved}" ]]; then
              resolved="/"
            fi
          fi
          ;;
        *)
          if [[ "${resolved}" == "/" ]]; then
            resolved="/${component}"
          else
            resolved="${resolved}/${component}"
          fi
          ;;
      esac
    done
  fi
  printf '%s\n' "${resolved}"
}

home_path="$(canonicalize_directory_target "${HOME}" "HOME")"
venv_path="$(canonicalize_directory_target "${venv_path}" "The Python environment")"
cache_home="$(canonicalize_directory_target "${cache_home}" "The package cache")"
source_dir="$(canonicalize_directory_target "${source_dir}" "The managed Proof Assistant source")"
repoprover_source="$(canonicalize_directory_target "${repoprover_source}" "The RepoProver checkout")"
elan_home="$(canonicalize_directory_target "${elan_home}" "The Lean toolchain")"
uv_home="$(canonicalize_directory_target "${uv_home}" "The uv bootstrap directory")"
if [[ -n "${project_root}" ]]; then
  project_root="$(canonicalize_directory_target "${project_root}" "The Proof Assistant source")"
fi

reject_dropbox_path() {
  local path="$1"
  local label="$2"
  case "${path}" in
    *[Dd][Rr][Oo][Pp][Bb][Oo][Xx]*)
      echo "ERROR: ${label} must not reside in Dropbox: ${path}" >&2
      return 2
      ;;
  esac
}

reject_dropbox_path "${venv_path}" "Python environments"
reject_dropbox_path "${cache_home}" "Package caches"
reject_dropbox_path "${source_dir}" "The managed Proof Assistant source"
reject_dropbox_path "${repoprover_source}" "The RepoProver checkout"
reject_dropbox_path "${elan_home}" "The Lean toolchain"
reject_dropbox_path "${uv_home}" "The uv bootstrap directory"
if [[ -n "${project_root}" ]]; then
  reject_dropbox_path "${project_root}" "The Proof Assistant source"
fi

case "${cache_home}" in
  "${home_path}"/*) ;;
  *)
    echo "ERROR: Package caches must reside inside the user home: ${cache_home}" >&2
    exit 2
    ;;
esac

export ELAN_HOME="${elan_home}"

normalize_repository_url() {
  local value="${1%/}"
  value="${value%.git}"
  case "${value}" in
    git@github.com:*) value="https://github.com/${value#git@github.com:}" ;;
    ssh://git@github.com/*) value="https://github.com/${value#ssh://git@github.com/}" ;;
  esac
  printf '%s\n' "${value}"
}

require_git() {
  local label="$1"
  if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: Git is required to install ${label}." >&2
    exit 2
  fi
}

require_clean_checkout() {
  local checkout="$1"
  local label="$2"
  if [[ -n "$(git -C "${checkout}" status --porcelain --untracked-files=all)" ]]; then
    echo "ERROR: ${label} checkout has local changes; preserve or move them before installing: ${checkout}" >&2
    exit 2
  fi
}

prepare_managed_checkout() {
  local checkout="$1"
  local url="$2"
  local ref="$3"
  local label="$4"
  local disable_push="$5"
  local actual_url repository_state resolved_ref
  local cloned=0

  require_git "${label}"
  repository_state="$(git -C "${checkout}" rev-parse --is-inside-work-tree 2>/dev/null || true)"
  if [[ -e "${checkout}" && "${repository_state}" != "true" ]]; then
    echo "ERROR: ${label} source path exists but is not a Git checkout: ${checkout}" >&2
    exit 2
  fi
  if [[ "${repository_state}" != "true" ]]; then
    mkdir -p "$(dirname "${checkout}")"
    if ! git clone --no-checkout "${url}" "${checkout}"; then
      echo "ERROR: Could not clone ${label} into ${checkout}" >&2
      exit 2
    fi
    cloned=1
  fi

  actual_url="$(git -C "${checkout}" remote get-url origin 2>/dev/null || true)"
  if [[ -z "${actual_url}" \
    || "$(normalize_repository_url "${actual_url}")" != "$(normalize_repository_url "${url}")" ]]; then
    echo "ERROR: ${label} checkout has an unexpected origin: ${actual_url:-<missing>}" >&2
    exit 2
  fi
  if [[ "${cloned}" -eq 0 ]]; then
    require_clean_checkout "${checkout}" "${label}"
  fi
  if ! git -C "${checkout}" fetch --depth=1 origin "${ref}"; then
    echo "ERROR: Could not fetch ${label} ref ${ref}" >&2
    exit 2
  fi
  resolved_ref="$(git -C "${checkout}" rev-parse FETCH_HEAD)"
  if ! git -C "${checkout}" checkout --detach "${resolved_ref}"; then
    echo "ERROR: Could not check out ${label} ref ${ref}" >&2
    exit 2
  fi
  require_clean_checkout "${checkout}" "${label}"
  if [[ "${disable_push}" == "yes" ]]; then
    git -C "${checkout}" remote set-url --push origin DISABLED
  fi
}

if [[ -z "${project_root}" ]]; then
  prepare_managed_checkout \
    "${source_dir}" "${repository_url}" "${install_ref}" "Proof Assistant" "no"
  exec "${source_dir}/install.sh"
fi

if [[ "${repoprover_source_was_explicit}" -eq 1 ]]; then
  require_git "RepoProver"
  if [[ "$(git -C "${repoprover_source}" rev-parse --is-inside-work-tree 2>/dev/null || true)" != "true" ]]; then
    echo "ERROR: PROOF_ASSISTANT_REPOPROVER_SOURCE must name an existing Git checkout: ${repoprover_source}" >&2
    exit 2
  fi
  require_clean_checkout "${repoprover_source}" "RepoProver"
  if [[ "$(git -C "${repoprover_source}" rev-parse HEAD)" != "${repoprover_ref}" ]]; then
    echo "ERROR: The explicit RepoProver checkout is not at the tested commit ${repoprover_ref}: ${repoprover_source}" >&2
    exit 2
  fi
else
  prepare_managed_checkout \
    "${repoprover_source}" "${repoprover_url}" "${repoprover_ref}" "RepoProver" "yes"
fi

elan_path_needs_shell=0
elan_bin="$(command -v elan 2>/dev/null || true)"
if [[ -z "${elan_bin}" && -x "${elan_home}/bin/elan" ]]; then
  elan_bin="${elan_home}/bin/elan"
  elan_path_needs_shell=1
fi
if [[ -z "${elan_bin}" ]] || ! "${elan_bin}" --version >/dev/null 2>&1; then
  echo "elan was not found; bootstrapping the pinned Lean toolchain manager."
  elan_installer_url="https://raw.githubusercontent.com/leanprover/elan/0e36a07b9bbcc5381fa6250df109f9a4f94d7bac/elan-init.sh"
  if command -v curl >/dev/null 2>&1; then
    if ! curl --proto '=https' --tlsv1.2 -sSfL "${elan_installer_url}" \
      | env ELAN_HOME="${elan_home}" sh -s -- \
        -y --no-modify-path --default-toolchain none; then
      echo "ERROR: Failed to download or run the pinned elan installer with curl." >&2
      exit 2
    fi
  elif command -v wget >/dev/null 2>&1; then
    if ! wget -qO- "${elan_installer_url}" \
      | env ELAN_HOME="${elan_home}" sh -s -- \
        -y --no-modify-path --default-toolchain none; then
      echo "ERROR: Failed to download or run the pinned elan installer with wget." >&2
      exit 2
    fi
  else
    echo "ERROR: Cannot bootstrap Lean because neither curl nor wget is installed." >&2
    exit 2
  fi
  elan_bin="${elan_home}/bin/elan"
  elan_path_needs_shell=1
fi
if [[ ! -x "${elan_bin}" ]] || ! "${elan_bin}" --version >/dev/null 2>&1; then
  echo "ERROR: A working elan executable is required." >&2
  exit 2
fi

lean_toolchain=""
IFS= read -r lean_toolchain < "${project_root}/lean-toolchain"
lean_toolchain="${lean_toolchain%$'\r'}"
if [[ -z "${lean_toolchain}" ]]; then
  echo "ERROR: The repository's lean-toolchain pin is empty." >&2
  exit 2
fi
if ! installed_toolchains="$("${elan_bin}" toolchain list)"; then
  echo "ERROR: Could not inspect installed Lean toolchains." >&2
  exit 2
fi
if awk -v expected="${lean_toolchain}" \
  '$1 == expected { found = 1 } END { exit(found ? 0 : 1) }' \
  <<< "${installed_toolchains}"; then
  echo "Using installed Lean toolchain: ${lean_toolchain}"
elif ! "${elan_bin}" toolchain install "${lean_toolchain}"; then
  echo "ERROR: Could not install the pinned Lean toolchain ${lean_toolchain}." >&2
  exit 2
fi
elan_bin_dir="$(cd "$(dirname "${elan_bin}")" && pwd)"
PATH="${elan_bin_dir}:${PATH}"
export PATH

uv_bin=""
uv_candidate="$(command -v uv 2>/dev/null || true)"
if [[ -n "${uv_candidate}" ]] && "${uv_candidate}" --version >/dev/null 2>&1; then
  uv_bin="${uv_candidate}"
elif [[ "${uv_candidate}" != "${uv_home}/uv" ]] \
  && [[ -x "${uv_home}/uv" ]] \
  && "${uv_home}/uv" --version >/dev/null 2>&1; then
  uv_bin="${uv_home}/uv"
fi
if [[ -n "${uv_bin}" ]]; then
  echo "Using uv: ${uv_bin}"
else
  if [[ -n "${uv_candidate}" ]]; then
    echo "Existing uv is not working; bootstrapping the checksum-verified project pin." >&2
  else
    echo "uv was not found; bootstrapping the checksum-verified project pin." >&2
  fi

  if ! mkdir -p "${uv_home}"; then
    echo "ERROR: Cannot create the uv bootstrap directory: ${uv_home}" >&2
    exit 2
  fi
  uv_bootstrap_script="${project_root}/scripts/bootstrap-uv.sh"
  if [[ ! -x "${uv_bootstrap_script}" ]]; then
    echo "ERROR: Missing executable uv bootstrap helper: ${uv_bootstrap_script}" >&2
    exit 2
  fi
  if ! uv_bin="$("${uv_bootstrap_script}" "${uv_home}")"; then
    echo "ERROR: The checksum-verified uv bootstrap failed." >&2
    exit 2
  fi
  if [[ -z "${uv_bin}" ]] || [[ ! -x "${uv_bin}" ]] \
    || ! "${uv_bin}" --version >/dev/null 2>&1; then
    echo "ERROR: The uv bootstrap did not produce a working executable in ${uv_home}" >&2
    exit 2
  fi
  echo "Bootstrapped uv: ${uv_bin}"
fi

# Use the exact executable discovered or returned by the verified bootstrap for
# every remaining uv operation; it does not need to be added to shell profiles.

mkdir -p "$(dirname "${venv_path}")"
if [[ ! -x "${venv_path}/bin/python" ]]; then
  "${uv_bin}" venv --python "${python_spec}" "${venv_path}"
fi

"${uv_bin}" pip install --python "${venv_path}/bin/python" \
  -e "${repoprover_source}" \
  -e "${project_root}[dev]"

# Installation is not considered successful merely because a compiler exists.
# This command compiles and executes a C program, and detects incompatible Lean
# bundled compilers before Lake starts expensive dependency work.
"${venv_path}/bin/proof-assistant" compiler-check
"${venv_path}/bin/proof-assistant" cache init
"${venv_path}/bin/python" -m pytest -q "${project_root}/tests"

startup_file_has_path() {
  local config="$1"
  local path_dir="$2"
  local path_line="$3"
  local legacy_path_line="$4"
  local home_relative="" home_ref="" braced_home_ref=""

  if grep -Fqx "${path_line}" "${config}" 2>/dev/null \
    || { [[ -n "${legacy_path_line}" ]] \
      && grep -Fqx "${legacy_path_line}" "${config}" 2>/dev/null; }; then
    return 0
  fi

  if [[ "${path_dir}" == "${HOME}/"* ]]; then
    home_relative="${path_dir#"${HOME}/"}"
    home_ref="\$HOME/${home_relative}"
    braced_home_ref="\${HOME}/${home_relative}"
  fi

  # Respect equivalent user-written PATH setup, including multiline guards and
  # portable $HOME spellings. Only assignments that prepend the directory to
  # the current PATH (or fish_add_path commands) count; mentions and removals do
  # not suppress the installer's idempotent guard.
  awk \
    -v absolute="${path_dir}" \
    -v home_ref="${home_ref}" \
    -v braced_home_ref="${braced_home_ref}" '
      function prepends_to_path(line, token, offset, position, suffix) {
        if (token == "") {
          return 0
        }
        offset = 1
        while ((position = index(substr(line, offset), token)) > 0) {
          position += offset - 1
          suffix = substr(line, position + length(token))
          if (index(suffix, ":$PATH") == 1 \
              || index(suffix, ":${PATH}") == 1 \
              || index(suffix, ":\"$PATH\"") == 1 \
              || index(suffix, ":\"${PATH}\"") == 1) {
            return 1
          }
          offset = position + length(token)
        }
        return 0
      }
      function names_target(line) {
        return index(line, absolute) > 0 \
          || (home_ref != "" && index(line, home_ref) > 0) \
          || (braced_home_ref != "" && index(line, braced_home_ref) > 0)
      }
      {
        code = $0
        comment = index(code, "#")
        if (comment > 0) {
          code = substr(code, 1, comment - 1)
        }
        command_count = split(code, commands, ";")
        for (command_index = 1; command_index <= command_count; command_index += 1) {
          command = commands[command_index]
          if (command ~ /^[[:space:]]*fish_add_path([[:space:]]|$)/ \
              && names_target(command)) {
            found = 1
          }
          if (command ~ /(^|[[:space:]])(export[[:space:]]+)?PATH[[:space:]]*=/ \
              && (prepends_to_path(command, absolute) \
                || prepends_to_path(command, home_ref) \
                || prepends_to_path(command, braced_home_ref))) {
            found = 1
          }
        }
      }
      END { exit(found ? 0 : 1) }
    ' "${config}" 2>/dev/null
}

configure_shell_path() {
  local shell_name="${SHELL:-sh}"
  shell_name="${shell_name##*/}"
  local path_dir="$1"
  local quoted_path config candidate config_root legacy_path_line path_line
  local legacy_backup legacy_suffix legacy_target managed_line managed_lines
  local configs=()

  # Use shell-specific startup files. Login shells and interactive shells can
  # read different files, so configure both where that distinction exists.
  printf -v quoted_path '%q' "${path_dir}"
  case "${shell_name}" in
    zsh)
      config_root="${ZDOTDIR:-${HOME}}"
      configs=("${config_root}/.zprofile" "${config_root}/.zshrc")
      path_line="case \":\$PATH:\" in *:${quoted_path}:*) ;; *) export PATH=${quoted_path}:\"\$PATH\";; esac"
      legacy_path_line="case \":\$PATH:\" in *\":${quoted_path}:\"*) ;; *) export PATH=${quoted_path}:\"\$PATH\";; esac"
      ;;
    bash)
      path_line="case \":\$PATH:\" in *:${quoted_path}:*) ;; *) export PATH=${quoted_path}:\"\$PATH\";; esac"
      legacy_path_line="case \":\$PATH:\" in *\":${quoted_path}:\"*) ;; *) export PATH=${quoted_path}:\"\$PATH\";; esac"

      # Releases before 0.1.0 could create a .bash_profile made entirely of
      # Proof Assistant PATH blocks. Transfer every recognized entry to the
      # next effective Bash login file before moving that owned-only profile.
      if [[ -e "${HOME}/.bash_profile" \
        && ! -f "${HOME}/.bash_profile" \
        && -r "${HOME}/.bash_profile" ]]; then
        echo "Refusing readable non-regular Bash startup file: ${HOME}/.bash_profile" >&2
        return 2
      fi
      if [[ -f "${HOME}/.bash_profile" \
        && ! -L "${HOME}/.bash_profile" \
        && -r "${HOME}/.bash_profile" ]] \
        && managed_lines="$(awk '
          function managed_guard(line, token, before, position) {
            if (index(line, export_prefix) == 1 \
                && substr(line, length(line) - length(export_suffix) + 1) == export_suffix) {
              token = substr(line, length(export_prefix) + 1, \
                length(line) - length(export_prefix) - length(export_suffix))
            } else {
              position = index(line, case_middle)
              if (index(line, case_prefix) != 1 || position == 0 \
                  || substr(line, length(line) - length(case_suffix) + 1) != case_suffix) {
                return ""
              }
              before = substr(line, 1, position - 1)
              token = substr(line, position + length(case_middle), \
                length(line) - position - length(case_middle) - length(case_suffix) + 1)
              if (before != case_prefix "*\":" token ":\"*" \
                  && before != case_prefix "*:" token ":*") {
                return ""
              }
            }
            if (token == "") {
              return ""
            }
            return case_prefix "*:" token ":*) ;; *) export PATH=" token export_suffix ";; esac"
          }
          BEGIN {
            marker = "# Added by Proof Assistant"
            installer_marker = "# Added by Proof Assistant installer"
            export_prefix = "export PATH="
            export_suffix = ":\"$PATH\""
            case_prefix = "case \":$PATH:\" in "
            case_middle = ") ;; *) export PATH="
            case_suffix = ":\"$PATH\";; esac"
            expect_marker = 1
            count = 0
            invalid = 0
          }
          /^[[:space:]]*$/ { next }
          expect_marker {
            if ($0 != marker && $0 != installer_marker) {
              invalid = 1
              exit 2
            }
            expect_marker = 0
            next
          }
          {
            guard = managed_guard($0)
            if (guard == "") {
              invalid = 1
              exit 2
            }
            print guard
            count += 1
            expect_marker = 1
          }
          END {
            if (invalid || !expect_marker || count == 0) {
              exit 2
            }
          }
        ' "${HOME}/.bash_profile")"; then
          legacy_target="${HOME}/.profile"
          for candidate in "${HOME}/.bash_login" "${HOME}/.profile"; do
            if [[ -L "${candidate}" && ! -e "${candidate}" ]]; then
              continue
            fi
            if [[ ! -e "${candidate}" ]]; then
              continue
            fi
            if [[ -f "${candidate}" ]]; then
              if [[ -r "${candidate}" ]]; then
                legacy_target="${candidate}"
                break
              fi
              continue
            fi
            if [[ -r "${candidate}" ]]; then
              echo "Refusing readable non-regular Bash startup file: ${candidate}" >&2
              return 2
            fi
          done
          if [[ -L "${legacy_target}" && ! -e "${legacy_target}" ]]; then
            echo "Refusing to update broken startup-file symlink: ${legacy_target}" >&2
            return 2
          fi
          if [[ -e "${legacy_target}" && ! -f "${legacy_target}" ]]; then
            echo "Refusing to update non-regular startup file: ${legacy_target}" >&2
            return 2
          fi
          if [[ -e "${legacy_target}" && ! -w "${legacy_target}" ]]; then
            echo "Cannot write shell startup file: ${legacy_target}" >&2
            return 2
          fi
          mkdir -p "$(dirname "${legacy_target}")"
          while IFS= read -r managed_line; do
            if ! grep -Fqx "${managed_line}" "${legacy_target}" 2>/dev/null; then
              printf '\n# Added by Proof Assistant installer\n%s\n' \
                "${managed_line}" >> "${legacy_target}"
            fi
          done <<< "${managed_lines}"

          legacy_backup="${HOME}/.bash_profile.proof-assistant-backup"
          legacy_suffix=0
          while [[ -e "${legacy_backup}" || -L "${legacy_backup}" ]]; do
            legacy_suffix=$((legacy_suffix + 1))
            legacy_backup="${HOME}/.bash_profile.proof-assistant-backup-${legacy_suffix}"
          done
          mv "${HOME}/.bash_profile" "${legacy_backup}"
          echo "Migrated legacy installer-only .bash_profile to ${legacy_backup}"
      fi

      # Bash reads only the first existing login profile in this order. Never
      # create .bash_profile or .bash_login here: doing so would shadow an
      # existing .profile and can suppress its .bashrc loader and other setup.
      config="${HOME}/.profile"
      for candidate in \
        "${HOME}/.bash_profile" \
        "${HOME}/.bash_login" \
        "${HOME}/.profile"; do
        if [[ -L "${candidate}" && ! -e "${candidate}" ]]; then
          continue
        fi
        if [[ ! -e "${candidate}" ]]; then
          continue
        fi
        if [[ -f "${candidate}" ]]; then
          if [[ -r "${candidate}" ]]; then
            config="${candidate}"
            break
          fi
          continue
        fi
        if [[ -r "${candidate}" ]]; then
          echo "Refusing readable non-regular Bash startup file: ${candidate}" >&2
          return 2
        fi
      done
      configs=("${config}" "${HOME}/.bashrc")
      ;;
    fish)
      config_root="${XDG_CONFIG_HOME:-${HOME}/.config}"
      config="${config_root}/fish/config.fish"
      configs=("${config}")
      printf -v quoted_path '%q' "${path_dir}"
      path_line="fish_add_path --path ${quoted_path}"
      legacy_path_line=""
      ;;
    *)
      configs=("${HOME}/.profile")
      path_line="case \":\$PATH:\" in *:${quoted_path}:*) ;; *) export PATH=${quoted_path}:\"\$PATH\";; esac"
      legacy_path_line="case \":\$PATH:\" in *\":${quoted_path}:\"*) ;; *) export PATH=${quoted_path}:\"\$PATH\";; esac"
      ;;
  esac

  for config in "${configs[@]}"; do
    if [[ -L "${config}" && ! -e "${config}" ]]; then
      echo "Refusing to update broken startup-file symlink: ${config}" >&2
      return 2
    fi
    if [[ -e "${config}" && ! -f "${config}" ]]; then
      echo "Refusing to update non-regular startup file: ${config}" >&2
      return 2
    fi
    if [[ -e "${config}" && ! -w "${config}" ]]; then
      echo "Cannot write shell startup file: ${config}" >&2
      return 2
    fi
    mkdir -p "$(dirname "${config}")"
    if ! startup_file_has_path \
      "${config}" "${path_dir}" "${path_line}" "${legacy_path_line}"; then
      printf '\n# Added by Proof Assistant installer\n%s\n' "${path_line}" >> "${config}"
    fi
  done
  echo "Added proof-assistant to ${shell_name} startup path (${configs[*]})"
}

if [[ "${elan_path_needs_shell}" -eq 1 ]]; then
  configure_shell_path "${elan_bin_dir}"
fi
configure_shell_path "${venv_path}/bin"
echo "Proof Assistant installation complete."
echo "Run now: ${venv_path}/bin/proof-assistant"
echo "New terminals can use: proof-assistant"
