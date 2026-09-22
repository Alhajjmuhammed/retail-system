#!/usr/bin/env bash
# Restoring.
#
# Written now rather than during the incident, because a backup nobody has
# ever restored is a hope, not a backup.
#
#   ops/restore.sh /var/backups/retail/retail-20260918-020000.dump.gz
#
# Like backup.sh, it works through the database container unless DATABASE_URL
# says otherwise. Restore into a spare database first and open the result;
# that is the only way to know the backups are real.

set -euo pipefail

DUMP="${1:?usage: restore.sh <dump.gz>}"
COMPOSE="${COMPOSE:-podman compose}"
DB_SERVICE="${DB_SERVICE:-db}"
# Restored as the application's own role, not the superuser. pg_dump is
# run with --no-owner, so whoever restores ends up owning every table --
# and if that is the superuser, the application comes back up to
# "permission denied for table accounts_user" on the first page it opens.
DB_USER="${DB_USER:-retail_app}"
DB_NAME="${DB_NAME:-retail}"

AS=""
[ -n "${PG_SUDO_USER:-}" ] && AS="sudo -u ${PG_SUDO_USER}"
# A machine with two clusters on it has two sets of clients, and the one
# on PATH is whichever Debian made the default -- pg_dump refuses to talk
# to a newer server. PG_BIN_DIR names the right one outright. It has to be
# a path, not an environment variable like PGCLUSTER: sudo drops those.
PG="${PG_BIN_DIR:+${PG_BIN_DIR%/}/}"

if [ -n "${DATABASE_URL:-}" ]; then
    TARGET="${DATABASE_URL%%\?*}"
    run_restore() { $AS ${PG}pg_restore --dbname="$DATABASE_URL" "$@"; }
else
    TARGET="${DB_NAME} in the ${DB_SERVICE} container"
    run_restore() { $COMPOSE exec -T "$DB_SERVICE" \
                        pg_restore --username="$DB_USER" --dbname="$DB_NAME" "$@"; }
fi

echo "This overwrites everything in ${TARGET}."
read -rp "Type the word restore to continue: " confirm
[ "$confirm" = "restore" ] || { echo "Stopped."; exit 1; }

# The web and worker containers hold open connections, and --clean cannot
# drop a table somebody is reading. Stop them, restore, start them again.
if [ -z "${DATABASE_URL:-}" ]; then
    $COMPOSE stop web worker beat
    trap '$COMPOSE start web worker beat' EXIT
fi

gunzip --stdout "$DUMP" | run_restore --clean --if-exists --no-owner

echo "Restored. Now run:"
echo "  podman compose exec web python manage.py migrate"
echo "  podman compose exec web python manage.py sync_permissions"
echo "  podman compose exec web python manage.py apply_rls"
