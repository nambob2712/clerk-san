#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
requested_backup_root="${1:-$root/backups/$(date -u +%Y%m%dT%H%M%SZ)}"
compose=(docker compose --project-directory "$root" --profile app)
stop_timeout="${CLERKSAN_BACKUP_STOP_TIMEOUT:-300}"
backup_mode="${CLERKSAN_BACKUP_MODE:-ordinary}"
running_services=()

command -v docker >/dev/null || {
  echo "Docker is required for a Compose backup. Use the SQLite demo backup command instead." >&2
  exit 1
}
[[ "$stop_timeout" =~ ^[1-9][0-9]*$ ]] || {
  echo "CLERKSAN_BACKUP_STOP_TIMEOUT must be a positive number of seconds." >&2
  exit 2
}
case "$backup_mode" in
  ordinary|maintenance) ;;
  *)
    echo "CLERKSAN_BACKUP_MODE must be ordinary or maintenance." >&2
    exit 2
    ;;
esac
[[ ! -L "$requested_backup_root" ]] || {
  echo "Backup destination must not be a symbolic link: $requested_backup_root" >&2
  exit 2
}
if [[ -e "$requested_backup_root" ]]; then
  [[ -d "$requested_backup_root" ]] || {
    echo "Backup destination is not a directory: $requested_backup_root" >&2
    exit 2
  }
  if ! existing_entry="$(find "$requested_backup_root" -mindepth 1 -maxdepth 1 -print -quit)"; then
    echo "Could not inspect backup destination: $requested_backup_root" >&2
    exit 2
  fi
  [[ -z "$existing_entry" ]] || {
    echo "Backup destination must be empty: $requested_backup_root" >&2
    exit 2
  }
else
  mkdir -p "$requested_backup_root"
fi
backup_root="$(CDPATH= cd -- "$requested_backup_root" && pwd -P)"
cd "$root"

service_is_running() {
  local service=$1
  local container_id
  if ! container_id="$("${compose[@]}" ps -q "$service")"; then
    echo "Could not determine whether $service is running; refusing an inconsistent backup." >&2
    return 2
  fi
  [[ -n "$container_id" ]]
}

resume_services() {
  local service
  local resumed_id
  if ((${#running_services[@]} == 0)); then
    return 0
  fi
  if ! "${compose[@]}" start "${running_services[@]}"; then
    return 1
  fi
  for service in "${running_services[@]}"; do
    if ! resumed_id="$("${compose[@]}" ps -q "$service")" || [[ -z "$resumed_id" ]]; then
      echo "Could not confirm that $service resumed after backup." >&2
      return 1
    fi
  done
}

resume_after_exit() {
  local status=$?
  trap - EXIT HUP INT TERM
  if ! resume_services; then
    echo "Backup stopped the API/worker, but could not restore their prior running state." >&2
    echo "Run: docker compose --profile app start ${running_services[*]}" >&2
    if [[ "$status" -eq 0 ]]; then
      status=1
    fi
  fi
  exit "$status"
}

copy_document_store() {
  local destination=$1

  # Do not bind-mount the host backup directory into a root-owned transient
  # container.  Stream a regular-file-only archive instead, so the host creates
  # its own backup artifacts under the caller's account.
  mkdir "$destination"
  "${compose[@]}" run --rm --no-deps -T api python -c '
from pathlib import Path
import sys
import tarfile

root = Path("/data/doc_store")
if root.is_symlink() or not root.is_dir():
    raise RuntimeError("document store is unavailable")

with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] == ".quarantine":
            if relative == Path(".quarantine") and (path.is_symlink() or not path.is_dir()):
                raise RuntimeError("document-store quarantine boundary is invalid")
            continue
        if relative == Path(".storage.lock"):
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("document-store lock boundary is invalid")
            continue
        if path.is_symlink():
            raise RuntimeError(f"document store contains a symbolic link: {path}")
        if not path.is_file() and not path.is_dir():
            raise RuntimeError(f"document store contains an unsupported entry: {path}")
        archive.add(path, arcname=relative.as_posix(), recursive=False)
' | python3 -c '
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile

destination = Path(sys.argv[1])
if destination.is_symlink() or not destination.is_dir():
    raise SystemExit(f"backup document-store destination is unavailable: {destination}")
root = destination.resolve()

with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as archive:
    for member in archive:
        raw_name = member.name
        relative = PurePosixPath(raw_name)
        parts = relative.parts
        if (
            not raw_name
            or raw_name.startswith("/")
            or not parts
            or any(part in ("", ".", "..") for part in parts)
            or parts[0] in (".quarantine", ".storage.lock")
        ):
            raise SystemExit(f"unsafe document-store archive member: {raw_name!r}")
        target = root.joinpath(*parts)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=False)
            continue
        if not member.isfile():
            raise SystemExit(f"unsupported document-store archive member: {raw_name!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise SystemExit(f"duplicate document-store archive member: {raw_name!r}")
        source = archive.extractfile(member)
        if source is None:
            raise SystemExit(f"could not read document-store archive member: {raw_name!r}")
        with source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
' "$destination"
}

for service in api worker; do
  if service_is_running "$service"; then
    running_services+=("$service")
  else
    service_status=$?
    if [[ "$service_status" -eq 2 ]]; then
      exit 1
    fi
  fi
done

if ((${#running_services[@]} > 0)); then
  trap resume_after_exit EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
fi

if ! "${compose[@]}" stop --timeout "$stop_timeout" api worker; then
  echo "Could not quiesce the API and worker; no backup was created." >&2
  exit 1
fi
for service in api worker; do
  if service_is_running "$service"; then
    echo "Could not quiesce $service; refusing an inconsistent backup." >&2
    exit 1
  else
    service_status=$?
    if [[ "$service_status" -eq 2 ]]; then
      exit 1
    fi
  fi
done

if ! "${compose[@]}" run --rm --no-deps -T api \
  python -m clerksan.tools.backup maintenance-preflight \
  --wait-seconds "$stop_timeout" >/dev/null; then
  echo "Maintenance preflight did not prove a clean lease/quarantine boundary." >&2
  exit 1
fi
if ! "${compose[@]}" run --rm --no-deps -T api \
  python -m clerksan.tools.backup database-inventory \
  >"$backup_root/database-inventory.json"; then
  echo "Could not record the sanitized database/store inventory." >&2
  exit 1
fi

"${compose[@]}" exec -T db pg_dump --clean --if-exists \
  -U "${POSTGRES_USER:-clerksan}" -d "${POSTGRES_DB:-clerksan}" >"$backup_root/database.sql"
if ! copy_document_store "$backup_root/doc_store"; then
  echo "Could not copy the document store through the transient API service." >&2
  exit 1
fi
python3 -m clerksan.tools.backup manifest "$backup_root"

if [[ "$backup_mode" == ordinary ]]; then
  if ! resume_services; then
    echo "Backup is valid, but the API/worker could not be restarted automatically." >&2
    exit 1
  fi
fi
trap - EXIT HUP INT TERM
if [[ "$backup_mode" == maintenance ]]; then
  echo "Backup written to $backup_root; maintenance fence remains closed."
else
  echo "Backup written to $backup_root"
fi
