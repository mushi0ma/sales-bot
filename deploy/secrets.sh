#!/usr/bin/env bash
# Secrets of the stack live ONLY in Secret Manager (deploy/secrets.map). Runs on the laptop (needs secretmanager.admin).
#   deploy/secrets.sh status         which secrets have a value (names only)
#   deploy/secrets.sh init           create missing secrets, fill every empty gen:* secret with a random value
#   deploy/secrets.sh import         copy env:* values from ./.env of this checkout (only empty secrets)
# Values travel through stdin only: never argv, never the terminal, never a file. Existing values are never rotated
# (a new ENCRYPTION_KEY would orphan the agents vault, a new DB password would lock out the existing volume).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
source "$here/gcp.env"
P=(--project="$GCP_PROJECT")
entries() { grep -vE '^\s*(#|$)' "$here/secrets.map"; }
has_value() { gcloud secrets versions access latest --secret="$1" "${P[@]}" </dev/null >/dev/null 2>&1; }
ensure_secret() {
  gcloud secrets describe "$1" "${P[@]}" </dev/null >/dev/null 2>&1 || gcloud secrets create "$1" "${P[@]}" \
    --replication-policy=user-managed --locations=europe-west4 --labels=app=salesbot --quiet </dev/null >/dev/null
}
put() { gcloud secrets versions add "$1" --data-file=- "${P[@]}" --quiet >/dev/null; }  # value on stdin
gen() {
  case "$1" in
    gen:hex:*) openssl rand -hex "${1#gen:hex:}" | tr -d '\n' ;;
    gen:sk)    printf 'sk-%s' "$(openssl rand -hex 32)" ;;
    gen:pass)  printf '%sAa1!' "$(openssl rand -hex 16)" ;;
    gen:pfx:*) p="${1#gen:pfx:}"; printf '%s%s' "${p%:*}" "$(openssl rand -hex "${p##*:}")" ;;
  esac
}
case "${1:-status}" in
  status)
    entries | while read -r name var src; do
      if has_value "$name"; then st=set; else st=EMPTY; fi
      printf '%-26s %-26s %-24s %s\n' "$name" "$var" "$src" "$st"
    done ;;
  init)
    entries | while read -r name var src; do
      ensure_secret "$name"
      [[ "$src" == gen:* ]] || continue
      if has_value "$name"; then echo "keep     $name"; else gen "$src" | put "$name"; echo "generated $name"; fi
    done ;;
  import)
    entries | while read -r name var src; do
      [[ "$src" == env:* ]] || continue
      ensure_secret "$name"
      if has_value "$name"; then echo "keep     $name"; continue; fi
      v="$(grep -E "^${src#env:}=" "$root/.env" 2>/dev/null | tail -1 | cut -d= -f2-)"
      if [[ -z "$v" ]]; then echo "skip     $name (${src#env:} not in ./.env)"; continue; fi
      printf '%s' "$v" | put "$name"; unset v; echo "imported $name"
    done ;;
  *) echo "usage: $0 status|init|import" >&2; exit 1 ;;
esac
