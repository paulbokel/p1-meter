#!/bin/sh
# Nightly pg_dump (NF-09). Runs inside the postgres image; keeps BACKUP_KEEP dumps.
set -u
: "${BACKUP_HOUR:=3}" "${BACKUP_KEEP:=14}"
export PGHOST="${PGHOST:-db}" PGUSER="$POSTGRES_USER" PGPASSWORD="$POSTGRES_PASSWORD" PGDATABASE="$POSTGRES_DB"

run_backup() {
  id=$(psql -qtAX -c "INSERT INTO backup_run DEFAULT VALUES RETURNING id")
  f="/backups/p1-$(date -u +%Y%m%dT%H%M%SZ).dump"
  if out=$(pg_dump -Fc -f "$f" 2>&1); then
    size=$(stat -c %s "$f")
    echo "UPDATE backup_run SET finished_at=now(), ok=true, file=:'f', size_bytes=:s WHERE id=:id;" |
      psql -qtAX -v id="$id" -v f="$f" -v s="$size"
    ls -1t /backups/p1-*.dump | tail -n +"$((BACKUP_KEEP + 1))" | xargs -r rm -f
    echo "backup ok: $f ($size bytes)"
  else
    rm -f "$f"
    echo "UPDATE backup_run SET finished_at=now(), ok=false, message=:'m' WHERE id=:id;" |
      psql -qtAX -v id="$id" -v m="$out"
    echo "backup FAILED: $out" >&2
  fi
}

[ "${1:-}" = "now" ] && { run_backup; exit; }
last=""
while true; do
  today=$(date +%F)
  if [ "$(date +%H)" -eq "$BACKUP_HOUR" ] && [ "$last" != "$today" ]; then
    run_backup
    last=$today
  fi
  sleep 60
done
