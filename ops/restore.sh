#!/usr/bin/env bash
# Restoring.
#
# Written now rather than during the incident, because a backup nobody has
# ever restored is a hope, not a backup.
#
#   ops/restore.sh /var/backups/retail/retail-20260918-020000.dump.gz

set -euo pipefail

DUMP="${1:?usage: restore.sh <dump.gz>}"
TARGET="${DATABASE_URL:?set DATABASE_URL}"

echo "This overwrites everything in ${TARGET%%\?*}."
read -rp "Type the word restore to continue: " confirm
[ "$confirm" = "restore" ] || { echo "Stopped."; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
gunzip --stdout "$DUMP" > "$WORK/db.dump"

pg_restore --clean --if-exists --no-owner --dbname="$TARGET" "$WORK/db.dump"

echo "Restored. Now run:"
echo "  python manage.py migrate"
echo "  python manage.py sync_permissions"
echo "  python manage.py apply_rls"
