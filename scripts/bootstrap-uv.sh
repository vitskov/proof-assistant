#!/usr/bin/env bash
set -euo pipefail

export UV_NO_MODIFY_PATH=1

readonly uv_version="0.12.0"
readonly project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly checksum_file="${project_root}/requirements/uv-${uv_version}-sha256.txt"
readonly destination="${1:-$(mktemp -d)}"
readonly profile_snapshot="$(mktemp)"
trap 'rm -f "${profile_snapshot}" "${profile_snapshot}.after"' EXIT

snapshot_profiles() {
  local output="$1"
  local profile
  : > "${output}"
  for profile in .bash_profile .bashrc .profile .zprofile .zshrc; do
    if [[ -f "${HOME}/${profile}" ]]; then
      if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "${HOME}/${profile}" >> "${output}"
      else
        shasum -a 256 "${HOME}/${profile}" >> "${output}"
      fi
    fi
  done
}

snapshot_profiles "${profile_snapshot}"

case "$(uname -s):$(uname -m)" in
  Linux:x86_64) uv_target="x86_64-unknown-linux-gnu" ;;
  Darwin:arm64) uv_target="aarch64-apple-darwin" ;;
  Darwin:x86_64) uv_target="x86_64-apple-darwin" ;;
  *)
    echo "ERROR: unsupported uv bootstrap platform: $(uname -s):$(uname -m)" >&2
    exit 2
    ;;
esac

readonly uv_target
readonly uv_asset="uv-${uv_target}.tar.gz"
readonly uv_release="https://github.com/astral-sh/uv/releases/download/${uv_version}"
readonly archive="${destination}/${uv_asset}"
readonly extracted_uv="${destination}/uv-${uv_target}/uv"

expected_sha="$(awk -v asset="${uv_asset}" '$2 == asset { print $1 }' "${checksum_file}")"
if [[ -z "${expected_sha}" ]]; then
  echo "ERROR: no checksum recorded for ${uv_asset}" >&2
  exit 2
fi

mkdir -p "${destination}"
curl --proto '=https' --tlsv1.2 -LsSf "${uv_release}/${uv_asset}" -o "${archive}"
if command -v sha256sum >/dev/null 2>&1; then
  actual_sha="$(sha256sum "${archive}" | awk '{ print $1 }')"
else
  actual_sha="$(shasum -a 256 "${archive}" | awk '{ print $1 }')"
fi
if [[ "${actual_sha}" != "${expected_sha}" ]]; then
  echo "ERROR: SHA-256 mismatch for ${uv_asset}" >&2
  exit 2
fi

tar -xzf "${archive}" -C "${destination}"
if [[ "$("${extracted_uv}" --version)" != "uv ${uv_version}"* ]]; then
  echo "ERROR: bootstrapped uv does not report version ${uv_version}" >&2
  exit 2
fi

snapshot_profiles "${profile_snapshot}.after"
if ! cmp -s "${profile_snapshot}" "${profile_snapshot}.after"; then
  echo "ERROR: uv bootstrap changed a shell startup file" >&2
  exit 2
fi

printf '%s\n' "${extracted_uv}"
