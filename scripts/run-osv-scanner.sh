#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
readonly version="2.5.1"
readonly release_url="https://github.com/google/osv-scanner/releases/download/v${version}"

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

platform="$(uname -s)-$(uname -m)"
case "$platform" in
  Darwin-arm64)
    asset="osv-scanner_darwin_arm64"
    expected="75c44d6332f892a1e56286f4105a98ed751ae28d215ca0a8b65cc00d84103054"
    ;;
  Darwin-x86_64)
    asset="osv-scanner_darwin_amd64"
    expected="9f89beb6c3d784893cb1cae0a3d56c529bfe91075418c2f9440c45b79654198b"
    ;;
  Linux-aarch64|Linux-arm64)
    asset="osv-scanner_linux_arm64"
    expected="3d0f5aa5a6baa8eb32bcef247388e149ef6030a6634ccae6fa0d62681fb27a6d"
    ;;
  Linux-x86_64|Linux-amd64)
    asset="osv-scanner_linux_amd64"
    expected="f9f25499a2c8cc367b3af45df2ea7eeca7fbccceab9c35079968f4b3652194be"
    ;;
  *) die "Unsupported OSV-Scanner platform: $platform" ;;
esac

sbom="${1:-$root/sbom/source-dependencies.spdx.json}"
[[ -f "$sbom" && ! -L "$sbom" ]] || die "Source dependency SBOM is missing or unsafe"
command -v curl >/dev/null || die "curl is required to obtain the pinned OSV-Scanner binary"
command -v shasum >/dev/null || die "shasum is required to verify the OSV-Scanner binary"

temporary="$(mktemp -d "${TMPDIR:-/tmp}/clerksan-osv.XXXXXX")"
case "$temporary" in
  "${TMPDIR:-/tmp}"/clerksan-osv.*) ;;
  *) die "Could not establish a bounded OSV-Scanner temporary directory" ;;
esac
cleanup() {
  rm -rf -- "$temporary"
}
trap cleanup EXIT

curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
  --output "$temporary/$asset" "$release_url/$asset"
printf '%s  %s\n' "$expected" "$temporary/$asset" | shasum -a 256 -c - >/dev/null
chmod 700 "$temporary/$asset"
"$temporary/$asset" scan source -L "$sbom"
