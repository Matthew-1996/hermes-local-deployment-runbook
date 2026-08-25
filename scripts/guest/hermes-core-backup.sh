#!/usr/bin/env bash
set -euo pipefail
umask 077

config_file=${HERMES_BACKUP_CONFIG:-/etc/hermes-backup.conf}
if [[ ! -r "$config_file" ]]; then
  echo "Missing readable backup config: $config_file" >&2
  exit 2
fi

# Expected variables:
# BACKUP_HOST, BACKUP_PORT, BACKUP_USER, BACKUP_KEY
# Optional: HERMES_ROOT
# shellcheck source=/dev/null
source "$config_file"

: "${BACKUP_HOST:?BACKUP_HOST is required}"
: "${BACKUP_PORT:?BACKUP_PORT is required}"
: "${BACKUP_USER:?BACKUP_USER is required}"
: "${BACKUP_KEY:?BACKUP_KEY is required}"

hermes_root=${HERMES_ROOT:-$HOME/.hermes}
stage=$(mktemp -d /tmp/hermes-core-backup.XXXXXX)
archive=$(mktemp /tmp/hermes-core-backup.XXXXXX.tar.gz)
trap 'rm -rf "$stage"; rm -f "$archive"' EXIT

mkdir -p "$stage/hermes"

core_items=(
  .env
  config.yaml
  SOUL.md
  auth.json
  channel_directory.json
  gateway_state.json
  feishu_seen_message_ids.json
  pairing
  memories
  skills
  sessions
  cron
  data
  state
  hooks
  kanban
  platforms
  scripts
  weixin
  pending_messages
)

for item in "${core_items[@]}"; do
  if [[ -e "$hermes_root/$item" ]]; then
    cp -a "$hermes_root/$item" "$stage/hermes/"
  fi
done

sqlite_backup() {
  local src=$1
  local dst=$2
  [[ -f "$src" ]] || return 0
  mkdir -p "$(dirname "$dst")"
  rm -f "$dst"
  python3 - "$src" "$dst" <<'PY'
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as source:
    with sqlite3.connect(dst) as target:
        source.backup(target)
PY
  chmod 600 "$dst"
}

sqlite_backup "$hermes_root/state.db" "$stage/hermes/state.db"
sqlite_backup "$hermes_root/kanban.db" "$stage/hermes/kanban.db"
sqlite_backup "$hermes_root/cron/executions.db" "$stage/hermes/cron/executions.db"

cat > "$stage/README.txt" <<EOF
Hermes core-data backup
Created: $(date --iso-8601=seconds)
Includes configuration, credentials, persona, memories, skills, sessions, cron state, pairing/platform state, and consistent SQLite snapshots.
Excludes reinstallable runtime, venv, Node.js, browser cache, local STT models, logs, and transient caches.
EOF

tar -C "$stage" -czf "$archive" README.txt hermes
tar -tzf "$archive" >/dev/null

reply=$(ssh -i "$BACKUP_KEY" \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes \
  -o ConnectTimeout=20 \
  -o ServerAliveInterval=60 \
  -o ServerAliveCountMax=3 \
  -p "$BACKUP_PORT" "$BACKUP_USER@$BACKUP_HOST" < "$archive")

printf '%s\n' "$reply"
printf 'LOCAL_ARCHIVE_BYTES=%s\n' "$(stat -c %s "$archive")"

