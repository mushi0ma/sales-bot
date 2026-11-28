#!/usr/bin/env bash
# An outbound call by the voice agent, for the ОСИ chairman: the connector rings the number from the test WhatsApp
# account and hands the voice to the agent with the campaign's prompt (campaigns.json). Run on the VM:
#   stack/voice/dial.sh <phone> <campaign> [name]      e.g. dial.sh +77011234567 votes Атанияз
# Only numbers of people who agreed to the test. 08:00-23:00 Astana (CALL_HOURS, the testers' day of group-watcher)
# unless FORCE=1. In Chatwoot the operator does the same with a private note `/call votes`. Several numbers at once:
# run it once per number (the agent carries VOICE_MAX_CALLS at a time). Results: the outcomes table in voice.db.
set -euo pipefail
cd "$(dirname "$0")/.."
phone="${1:-}"; phone="${phone//[^0-9]/}"; campaign="${2:-}"; name="${3:-}"
[[ ${#phone} -ge 11 && ${#phone} -le 15 && -n "$campaign" ]] || { echo "usage: dial.sh <phone> <campaign> [name]" >&2; exit 2; }
python3 -I -c 'import json,sys; c=json.load(open(sys.argv[1])); sys.exit(0 if sys.argv[2] in c and not sys.argv[2].startswith("_") else 1)' \
  voice/campaigns.json "$campaign" || { echo "unknown campaign: $campaign" >&2; exit 2; }
hour=$(TZ=Asia/Almaty date +%-H); hours="${CALL_HOURS:-8-23}"
if [[ "${FORCE:-}" != 1 ]] && (( hour < ${hours%-*} || hour >= ${hours#*-} )); then echo "quiet hours in Astana ($hour:00, calls $hours); FORCE=1 to override" >&2; exit 3; fi
name_q=$(python3 -I -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$name")
export PHONE="$phone" URL="ws://voice:8090/wa?campaign=$campaign&name=$name_q" KEY="$(date +%s%N)-$phone" INBOX="${WA_INBOX_ID:-2}"
docker compose exec -T -e PHONE -e URL -e KEY -e INBOX cw-rails bundle exec rails runner - 2>&1 <<'RUBY' | grep -E "^(placed|failed)"
begin
  r = Inbox.find(ENV.fetch('INBOX')).channel.provider_service.start_call_with_stream(ENV['PHONE'], ENV['URL'], ENV['KEY'])
  puts "placed call_id=#{r.is_a?(Hash) ? r['call_id'] : r.inspect}"
rescue StandardError => e
  puts "failed: #{e.class}: #{e.message[0, 200]}"
end
RUBY
