#!/usr/bin/env bash
set -euo pipefail
umask 077

backup_dir=${HERMES_BACKUP_DIR:-/srv/hermes-backups}
threshold_kb=${HERMES_BACKUP_TWO_COPY_THRESHOLD_KB:-10485760}
stamp=$(date +%Y%m%d-%H%M%S)
tmp=$backup_dir/.incoming-$stamp-$$.tar.gz
final=$backup_dir/hermes-local-$stamp.tar.gz

mkdir -p "$backup_dir"
trap 'rm -f "$tmp"' EXIT

timeout 1800 cat > "$tmp"
test -s "$tmp"
tar -tzf "$tmp" >/dev/null
mv "$tmp" "$final"
sha256sum "$final" > "$final.sha256"
chmod 600 "$final" "$final.sha256"

avail_kb=$(df -Pk "$backup_dir" | awk 'NR == 2 {print $4}')
if [[ "$avail_kb" -ge "$threshold_kb" ]]; then
  keep=2
else
  keep=1
fi

mapfile -t old_archives < <(
  find "$backup_dir" -maxdepth 1 -type f \
    -name 'hermes-local-*.tar.gz' -printf '%T@ %p\n' |
    sort -nr |
    awk -v keep="$keep" 'NR > keep {sub(/^[^ ]+ /, ""); print}'
)

for old in "${old_archives[@]}"; do
  rm -f -- "$old" "$old.sha256"
done

printf 'BACKUP_OK %s %s KEEP=%s AVAILABLE_KB=%s\n' \
  "$(basename "$final")" "$(stat -c %s "$final")" "$keep" "$avail_kb"

