#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
readonly version="8.30.1"
readonly release_url="https://github.com/gitleaks/gitleaks/releases/download/v${version}"
readonly config="$root/.gitleaks.toml"

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

platform="$(uname -s)-$(uname -m)"
case "$platform" in
  Darwin-arm64)
    archive="gitleaks_${version}_darwin_arm64.tar.gz"
    expected="b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"
    ;;
  Darwin-x86_64)
    archive="gitleaks_${version}_darwin_x64.tar.gz"
    expected="dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"
    ;;
  Linux-aarch64|Linux-arm64)
    archive="gitleaks_${version}_linux_arm64.tar.gz"
    expected="e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"
    ;;
  Linux-x86_64|Linux-amd64)
    archive="gitleaks_${version}_linux_x64.tar.gz"
    expected="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
    ;;
  *) die "Unsupported Gitleaks platform: $platform" ;;
esac

command -v curl >/dev/null || die "curl is required to obtain the pinned Gitleaks binary"
command -v tar >/dev/null || die "tar is required to unpack the pinned Gitleaks binary"
command -v shasum >/dev/null || die "shasum is required to verify the Gitleaks binary"
[[ -f "$config" && ! -L "$config" ]] || die "Gitleaks policy is missing or unsafe"

temporary="$(mktemp -d "${TMPDIR:-/tmp}/clerksan-gitleaks.XXXXXX")"
case "$temporary" in
  "${TMPDIR:-/tmp}"/clerksan-gitleaks.*) ;;
  *) die "Could not establish a bounded Gitleaks temporary directory" ;;
esac
cleanup() {
  rm -rf -- "$temporary"
}
trap cleanup EXIT

curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
  --output "$temporary/$archive" "$release_url/$archive"
printf '%s  %s\n' "$expected" "$temporary/$archive" | shasum -a 256 -c - >/dev/null
tar -xzf "$temporary/$archive" -C "$temporary" gitleaks
chmod 700 "$temporary/gitleaks"

command_name="${1:-}"
case "$command_name" in
  self-test)
    fixture="$temporary/self-test"
    mkdir "$fixture"
    marker="$(printf '%s%s%s' 'CLERKSAN_TEST_' 'SECRET_' 'A1b2C3d4E5f6G7h8I9j0')"
    printf '%s\n' "$marker" >"$fixture/input.txt"
    set +e
    "$temporary/gitleaks" dir --no-banner --redact=100 --config "$config" \
      --report-format json --report-path "$temporary/report.json" "$fixture" \
      >"$temporary/stdout" 2>"$temporary/stderr"
    result=$?
    set -e
    [[ "$result" == 1 ]] || die "Gitleaks self-test did not detect its synthetic marker"
    if grep -Fq -- "$marker" "$temporary/stdout" "$temporary/stderr" "$temporary/report.json"; then
      die "Gitleaks self-test exposed the synthetic marker instead of redacting it"
    fi
    printf '%s\n' "Gitleaks ${version}: detection and redaction self-test passed"
    ;;
  dir)
    target="${2:-$root}"
    "$temporary/gitleaks" dir --no-banner --redact=100 --config "$config" "$target"
    ;;
  git-range)
    repository="${2:-}"
    revision_range="${3:-}"
    [[ -d "$repository" && ! -L "$repository" ]] || die "Gitleaks repository is unavailable"
    [[ "$revision_range" =~ ^[0-9a-f]{40}\.\.[0-9a-f]{40}$ ]] || \
      die "Gitleaks revision range must contain exact SHA-1 commit IDs"
    (
      cd "$repository"
      "$temporary/gitleaks" git --no-banner --redact=100 --config "$config" \
        --log-opts="$revision_range" .
    )
    ;;
  *)
    die "Usage: scripts/run-gitleaks.sh self-test | dir [PATH] | git-range REPOSITORY BASE..HEAD"
    ;;
esac
