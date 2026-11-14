#!/bin/sh
set -eu
umask 077

# Compose reads dotenv itself. Host execution reads only these path options, never sources shell code.
read_option() {
    sed -n "s/^$1=//p" .env | tail -n 1 | sed 's/[[:space:]]*#.*$//;s/^"//;s/"$//;s/^'\''//;s/'\''$//'
}
if [ "${1:-}" != "--container" ] && [ "${1:-}" != "--schedule" ] && [ -f .env ]; then
    BACKUP_DIR="${BACKUP_DIR:-$(read_option BACKUP_DIR)}"
    BACKUP_MOUNT="${BACKUP_MOUNT:-$(read_option BACKUP_MOUNT)}"
fi
BACKUP_DIR="${BACKUP_DIR:-./backups}"
if [ -n "${BACKUP_MOUNT:-}" ]; then
    if ! mountpoint -q "$BACKUP_MOUNT"; then
        printf 'Backup refused: SSD mount is missing: %s\n' "$BACKUP_MOUNT" >&2
        exit 1
    fi
fi
if [ ! -d "$BACKUP_DIR" ]; then
    printf 'Backup refused: directory does not exist: %s (create it after checking the SSD mount)\n' "$BACKUP_DIR" >&2
    exit 1
fi

dump_file=""
partial_file=""
cleanup() {
    [ -z "$dump_file" ] || rm -f "$dump_file"
    [ -z "$partial_file" ] || rm -f "$partial_file"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

backup_once() (
    set -eu
    trap cleanup EXIT
    stamp="$(date -u +%F)"
    dump_file="$BACKUP_DIR/.streamclock-$stamp.sql.partial.$$"
    partial_file="$BACKUP_DIR/.streamclock-$stamp.sql.gz.partial.$$"
    final_file="$BACKUP_DIR/streamclock-$stamp.sql.gz"

    # Separate steps avoid hiding a failed pg_dump behind a successful gzip on POSIX shells.
    if [ "${1:-}" = "--container" ]; then
        pg_dump -h "${PGHOST:-db}" -U "${PGUSER:-streamclock}" "${PGDATABASE:-streamclock}" > "$dump_file" || exit 1
    else
        docker compose exec -T db sh -c \
            'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$dump_file" || exit 1
    fi
    gzip -c "$dump_file" > "$partial_file" || exit 1
    gzip -t "$partial_file" || exit 1
    mv -f "$partial_file" "$final_file" || exit 1
    find "$BACKUP_DIR" -type f -name 'streamclock-*.sql.gz' -mtime +30 -delete || exit 1
    printf 'Backup saved: %s\n' "$final_file"
)

if [ "${1:-}" = "--schedule" ]; then
    while :; do
        # 03:00 UTC every night, including after container restarts.
        delay=$(( (10800 - $(date -u +%s) % 86400 + 86400) % 86400 ))
        [ "$delay" -ne 0 ] || delay=86400
        sleep "$delay" &
        wait "$!"
        if ! backup_once --container; then
            printf 'Nightly backup FAILED; check storage and database. Will retry next night.\n' >&2
        fi
    done
else
    backup_once "${1:-}"
fi
