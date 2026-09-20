#!/usr/bin/env bash
# Nightly backup.
#
# A retail system holds the only record of what a shop sold. Losing it is not
# an inconvenience, it is the end of the business relationship. So: dump,
# verify the dump is readable, copy it off the machine, and keep a month.
#
#   0 2 * * * /app/ops/backup.sh >> /var/log/retail-backup.log 2>&1

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/retail}"
KEEP_DAYS="${KEEP_DAYS:-30}"
STAMP="$(date +%Y%m%d-%H%M%S)"
FILE="${BACKUP_DIR}/retail-${STAMP}.dump"

mkdir -p "$BACKUP_DIR"

echo "[$(date -Is)] dumping to ${FILE}"
pg_dump --format=custom --no-owner --dbname="${DATABASE_URL:?set DATABASE_URL}" --file="$FILE"

# A dump that cannot be listed cannot be restored. Check before trusting it.
if ! pg_restore --list "$FILE" > /dev/null 2>&1; then
    echo "[$(date -Is)] DUMP IS UNREADABLE, keeping yesterday's" >&2
    rm -f "$FILE"
    exit 1
fi

gzip --force "$FILE"
echo "[$(date -Is)] ok: $(du -h "${FILE}.gz" | cut -f1)"

# Off the machine. A backup sitting on the server that dies is not a backup.
if [ -n "${BACKUP_REMOTE:-}" ]; then
    echo "[$(date -Is)] copying to ${BACKUP_REMOTE}"
    rsync --archive --quiet "${FILE}.gz" "${BACKUP_REMOTE}/"
else
    echo "[$(date -Is)] WARNING: BACKUP_REMOTE is not set, this backup only exists here" >&2
fi

find "$BACKUP_DIR" -name 'retail-*.dump.gz' -mtime "+${KEEP_DAYS}" -delete
echo "[$(date -Is)] done"
