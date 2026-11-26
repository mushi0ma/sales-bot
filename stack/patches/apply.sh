#!/usr/bin/env bash
# Apply our local patches to the upstream clones in vendor/ (gitignored). Idempotent: an already
# applied patch is skipped. Run after every `git pull` in vendor/<repo>, before `docker compose build`.
#   stack/patches/<repo>-<name>.patch  ->  vendor/<repo> (the longest repo name that prefixes the file)
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
for p in "$root"/stack/patches/*.patch; do
  # The longest vendor/<repo> the file name starts with: whatsapp-connector-x.patch is not vendor/whatsapp.
  repo=""
  for d in "$root"/vendor/*/; do
    d="$(basename "$d")"
    [[ "$(basename "$p")" == "$d-"* && ${#d} -gt ${#repo} ]] && repo="$d"
  done
  [[ -n "$repo" ]] || { echo "no vendor/ repo for $(basename "$p")" >&2; exit 1; }
  dir="$root/vendor/$repo"
  if git -C "$dir" apply --reverse --check "$p" 2>/dev/null; then
    echo "already applied: $(basename "$p")"
  elif git -C "$dir" apply --check "$p"; then
    git -C "$dir" apply "$p" && echo "applied: $(basename "$p")"
  else
    echo "CONFLICT: $(basename "$p") no longer applies to vendor/$repo — rebase it" >&2; exit 1
  fi
done
