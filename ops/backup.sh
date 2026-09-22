#!/usr/bin/env bash
# Nightly backup.
#
# A retail system holds the only record of what a shop sold. Losing it is not
# an inconvenience, it is the end of the business relationship. So: dump,
# verify the dump is readable, copy it off the machine, and keep a month.
#
#   0 2 * * * cd /opt/retail && BACKUP_REMOTE=you@host:/backups ops/backup.sh
#
# By default it dumps through the database container, because that is where
# the database is: compose publishes no port for it, so nothing on the host
# can reach it, and the host may not have pg_dump at all. Set DATABASE_URL to
# dump directly instead -- for a database that is not in a container, or from
# a developer machine.

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/retail}"
KEEP_DAYS="${KEEP_DAYS:-30}"
COMPOSE="${COMPOSE:-podman compose}"
DB_SERVICE="${DB_SERVICE:-db}"
# Dumped by the superuser on purpose: row-level security applies to the
# application's own role, and a dump taken as retail_app would either be
# refused or come back with no rows in it.
DB_USER="${DB_USER:-retail}"
DB_NAME="${DB_NAME:-retail}"
STAMP="$(date +%Y%m%d-%H%M%S)"
FILE="${BACKUP_DIR}/retail-${STAMP}.dump"

# Both clients run in the same place, so their versions always match the
# server's -- a mismatch is the usual reason a dump refuses to restore.
if [ -n "${DATABASE_URL:-}" ]; then
    run_dump()    { pg_dump --format=custom --no-owner --dbname="$DATABASE_URL"; }
    run_restore() { pg_restore "$@"; }
else
    run_dump()    { $COMPOSE exec -T "$DB_SERVICE" \
                        pg_dump --format=custom --no-owner \
                                --username="$DB_USER" "$DB_NAME"; }
    run_restore() { $COMPOSE exec -T "$DB_SERVICE" pg_restore "$@"; }
fi

mkdir -p "$BACKUP_DIR"

echo "[$(date -Is)] dumping to ${FILE}"
# A failed dump still leaves a partial file behind, and a partial file that
# nobody deletes is the one somebody reaches for during an incident.
if ! run_dump > "$FILE"; then
    echo "[$(date -Is)] DUMP FAILED, keeping yesterday's" >&2
    rm -f "$FILE"
    exit 1
fi

# A dump that cannot be listed cannot be restored. Check before trusting it,
# and before deleting anything older.
if ! run_restore --list < "$FILE" > /dev/null 2>&1; then
    echo "[$(date -Is)] DUMP IS UNREADABLE, keeping yesterday's" >&2
    rm -f "$FILE"
    exit 1
fi

# An empty or truncated dump lists fine. A real one is never this small.
if [ "$(stat -c %s "$FILE")" -lt 10000 ]; then
    echo "[$(date -Is)] DUMP IS SUSPICIOUSLY SMALL, keeping yesterday's" >&2
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
