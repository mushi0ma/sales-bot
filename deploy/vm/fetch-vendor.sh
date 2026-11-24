#!/usr/bin/env bash
# Clone the upstream sources at the commits in deploy/vendor.lock into $VM_ROOT/vendor and apply stack/patches.
# Idempotent: a repo already at its commit is left alone (patches are re-checked by apply.sh).
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$root/vendor"
grep -vE '^\s*(#|$)' "$root/deploy/vendor.lock" | while read -r name url sha; do
  dir="$root/vendor/$name"
  if [[ "$(git -C "$dir" rev-parse HEAD 2>/dev/null)" != "$sha" ]]; then
    rm -rf "$dir"; git init -q "$dir"
    git -C "$dir" remote add origin "$url"
    git -C "$dir" fetch -q --depth 1 origin "$sha"
    git -C "$dir" -c advice.detachedHead=false checkout -q FETCH_HEAD
    echo "fetched $name @ ${sha:0:12}"
  else
    echo "ok $name @ ${sha:0:12}"
  fi
done
bash "$root/stack/patches/apply.sh"
