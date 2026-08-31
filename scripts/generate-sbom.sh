#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output=${1:-"$repo_root/sbom/source-dependencies.spdx.json"}
temporary="${output}.tmp.$$"
trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
mkdir -p -- "$(dirname -- "$output")"

SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-0}
export SOURCE_DATE_EPOCH
python3 "$repo_root/scripts/check-license-policy.py" --write-sbom "$temporary"

mv -- "$temporary" "$output"
trap - EXIT HUP INT TERM
