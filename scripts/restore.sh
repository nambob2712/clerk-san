#!/usr/bin/env bash
set -euo pipefail

if [[ "${CLERKSAN_RESTORE_CONFIRM:-}" != "ERASE_LOCAL_DATA" ]]; then
  echo "Refusing destructive restore. Set CLERKSAN_RESTORE_CONFIRM=ERASE_LOCAL_DATA." >&2
  exit 2
fi

requested_source_root="${1:?Usage: CLERKSAN_RESTORE_CONFIRM=ERASE_LOCAL_DATA scripts/restore.sh <backup-dir>}"
source_root="$(CDPATH= cd -- "$requested_source_root" && pwd -P)" || {
  echo "Backup directory does not exist: $requested_source_root" >&2
  exit 1
}
root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
compose=(docker compose --project-directory "$root" --profile app)
restore_mode="${CLERKSAN_RESTORE_MODE:-ordinary}"

command -v docker >/dev/null || {
  echo "Docker is required for a Compose restore. Use the SQLite demo restore command instead." >&2
  exit 1
}
case "$restore_mode" in
  ordinary|maintenance) ;;
  *)
    echo "CLERKSAN_RESTORE_MODE must be ordinary or maintenance." >&2
    exit 2
    ;;
esac
cd "$root"
python3 -m clerksan.tools.backup verify "$source_root"
[[ -f "$source_root/database.sql" ]] || {
  echo "Backup is missing database.sql" >&2
  exit 1
}
[[ -d "$source_root/doc_store" ]] || {
  echo "Backup is missing doc_store" >&2
  exit 1
}
if [[ "$restore_mode" == maintenance && ! -f "$source_root/database-inventory.json" ]]; then
  echo "Maintenance restore requires database-inventory.json from a fenced backup." >&2
  exit 1
fi

stop_timeout="${CLERKSAN_RESTORE_STOP_TIMEOUT:-300}"
running_services=()

[[ "$stop_timeout" =~ ^[1-9][0-9]*$ ]] || {
  echo "CLERKSAN_RESTORE_STOP_TIMEOUT must be a positive number of seconds." >&2
  exit 2
}

service_is_running() {
  local service=$1
  local container_id
  if ! container_id="$("${compose[@]}" ps -q "$service")"; then
    echo "Could not determine whether $service is running; refusing an unsafe restore." >&2
    return 2
  fi
  [[ -n "$container_id" ]]
}

ensure_database_ready() {
  local database_id
  if ! database_id="$("${compose[@]}" ps -q db)"; then
    echo "Could not determine whether PostgreSQL is running; refusing the restore." >&2
    return 1
  fi
  if [[ -z "$database_id" ]]; then
    echo "PostgreSQL is not running. Start data services first: docker compose up -d db ollama" >&2
    return 1
  fi
  if ! "${compose[@]}" exec -T db psql -v ON_ERROR_STOP=1 \
    -U "${POSTGRES_USER:-clerksan}" -d "${POSTGRES_DB:-clerksan}" \
    -c "SELECT 1" >/dev/null; then
    echo "PostgreSQL is not ready for the target database. Wait for it, then retry." >&2
    return 1
  fi
}

resume_previously_running_services() {
  local service
  local resumed_id
  if [[ "$restore_mode" == maintenance ]]; then
    return 0
  fi
  if ((${#running_services[@]} == 0)); then
    return 0
  fi
  if ! "${compose[@]}" up -d "${running_services[@]}"; then
    return 1
  fi
  for service in "${running_services[@]}"; do
    if ! resumed_id="$("${compose[@]}" ps -q "$service")" || [[ -z "$resumed_id" ]]; then
      echo "Could not confirm that $service resumed after restore." >&2
      return 1
    fi
  done
}

ensure_database_ready || exit 1
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

restore_tag=".restore-stage-$$"
previous_tag=".restore-previous-$$"
restore_active=false
database_restored=false
stage_attempted=false

stage_store() {
  "${compose[@]}" run --rm --no-deps \
    -e "RESTORE_TAG=$restore_tag" \
    -v "$source_root/doc_store:/restore:ro" \
    api sh -ceu '
      stage="/data/doc_store/$RESTORE_TAG"
      rm -rf "$stage"
      mkdir "$stage"
      cp -R /restore/. "$stage/"
    '
}

activate_store() {
  "${compose[@]}" run --rm --no-deps \
    -e "RESTORE_TAG=$restore_tag" \
    -e "PREVIOUS_TAG=$previous_tag" \
    api sh -ceu '
      root=/data/doc_store
      stage="$root/$RESTORE_TAG"
      previous="$root/$PREVIOUS_TAG"
      state_file="$previous/.restore-state"

      move_contents() {
        source=$1
        destination=$2
        first_excluded=${3:-}
        second_excluded=${4:-}
        for item in "$source"/.[!.]* "$source"/..?* "$source"/*; do
          [ -e "$item" ] || [ -L "$item" ] || continue
          name=${item##*/}
          [ "$name" = "$first_excluded" ] && continue
          [ "$name" = "$second_excluded" ] && continue
          mv -- "$item" "$destination"/ || return 1
        done
      }

      test -d "$stage"
      test ! -e "$previous"
      mkdir "$previous"
      printf "%s\n" moving-old >"$state_file"
      move_contents "$root" "$previous" "$RESTORE_TAG" "$PREVIOUS_TAG"
      printf "%s\n" moving-new >"$state_file"
      move_contents "$stage" "$root"
      rmdir "$stage"
      printf "%s\n" replacement-active >"$state_file"
    '
}

rollback_store() {
  "${compose[@]}" run --rm --no-deps \
    -e "PREVIOUS_TAG=$previous_tag" \
    api sh -ceu '
      root=/data/doc_store
      previous="$root/$PREVIOUS_TAG"
      state_file="$previous/.restore-state"
      move_contents() {
        source=$1
        destination=$2
        for item in "$source"/.[!.]* "$source"/..?* "$source"/*; do
          [ -e "$item" ] || [ -L "$item" ] || continue
          mv -- "$item" "$destination"/ || return 1
        done
      }
      remove_replacement() {
        for item in "$root"/.[!.]* "$root"/..?* "$root"/*; do
          [ -e "$item" ] || [ -L "$item" ] || continue
          [ "${item##*/}" = "$PREVIOUS_TAG" ] && continue
          rm -rf -- "$item" || return 1
        done
      }
      [ -e "$previous" ] || exit 0
      [ -f "$state_file" ] || {
        echo "Restore safety state is missing from $previous." >&2
        exit 1
      }
      state=$(cat "$state_file")
      case "$state" in
        moving-old)
          rm "$state_file"
          move_contents "$previous" "$root"
          rmdir "$previous"
          ;;
        moving-new|replacement-active)
          remove_replacement
          rm "$state_file"
          move_contents "$previous" "$root"
          rmdir "$previous"
          ;;
        *)
          echo "Unknown restore safety state: $state" >&2
          exit 1
          ;;
      esac
    '
}

discard_stage() {
  "${compose[@]}" run --rm --no-deps \
    -e "RESTORE_TAG=$restore_tag" \
    api sh -ceu 'rm -rf "/data/doc_store/$RESTORE_TAG"'
}

discard_previous_store() {
  "${compose[@]}" run --rm --no-deps \
    -e "PREVIOUS_TAG=$previous_tag" \
    api sh -ceu 'rm -rf "/data/doc_store/$PREVIOUS_TAG"'
}

verify_restored_inventory() {
  if [[ -f "$source_root/database-inventory.json" ]]; then
    "${compose[@]}" run --rm --no-deps -T \
      -v "$source_root/database-inventory.json:/restore-inventory.json:ro" \
      api python -m clerksan.tools.backup database-inventory \
      --verify /restore-inventory.json \
      --excluded-restore-entry "$previous_tag" >/dev/null
  else
    # Older ordinary backups have no logical inventory. Still verify every current
    # database artifact reference and hash before removing the prior store.
    "${compose[@]}" run --rm --no-deps -T api \
      python -m clerksan.tools.backup database-inventory \
      --excluded-restore-entry "$previous_tag" >/dev/null
  fi
}

recover_after_failure() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [[ "$restore_active" == true ]]; then
    if [[ "$database_restored" == false ]]; then
      if ! rollback_store; then
        echo "Restore failed and the prior document store could not be restored automatically." >&2
        echo "Leave the API and worker stopped; recover /data/doc_store/$previous_tag before retrying." >&2
        exit "$status"
      fi
    else
      echo "Restored data failed verification; the prior store remains retained." >&2
      echo "Leave writers stopped and inspect /data/doc_store/$previous_tag before recovery." >&2
      exit "$status"
    fi
  fi
  if [[ "$stage_attempted" == true ]]; then
    discard_stage || true
  fi
  if ! resume_previously_running_services; then
    echo "Restore could not restore the prior API/worker running state." >&2
    echo "Run: docker compose --profile app up -d ${running_services[*]}" >&2
  fi
  exit "$status"
}

trap recover_after_failure EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if ! "${compose[@]}" stop --timeout "$stop_timeout" api worker; then
  echo "Could not quiesce the API and worker; restore did not begin." >&2
  exit 1
fi
for service in api worker; do
  if service_is_running "$service"; then
    echo "Could not quiesce $service; refusing an unsafe restore." >&2
    exit 1
  else
    service_status=$?
    if [[ "$service_status" -eq 2 ]]; then
      exit 1
    fi
  fi
done

stage_attempted=true
stage_store
restore_active=true
activate_store

if ! "${compose[@]}" exec -T db psql \
  -v ON_ERROR_STOP=1 \
  --single-transaction \
  -U "${POSTGRES_USER:-clerksan}" \
  -d "${POSTGRES_DB:-clerksan}" <"$source_root/database.sql"; then
  echo "Database restore failed; rolling the document store back." >&2
  exit 1
fi

database_restored=true
if ! verify_restored_inventory; then
  echo "Restore inventory verification failed; prior storage was retained." >&2
  exit 1
fi
restore_active=false
if ! discard_previous_store; then
  echo "Restore completed, but the retained pre-restore document store could not be removed." >&2
  echo "It is safe to remove /data/doc_store/$previous_tag after verifying the restore." >&2
fi
if [[ "$restore_mode" == ordinary ]]; then
  if ! resume_previously_running_services; then
    echo "Restore completed, but the prior API/worker running state could not be restored." >&2
    exit 1
  fi
fi
trap - EXIT HUP INT TERM
if [[ "$restore_mode" == maintenance ]]; then
  echo "Restore completed from $source_root; maintenance fence remains closed."
else
  echo "Restore completed from $source_root."
fi
