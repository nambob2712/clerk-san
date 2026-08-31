#!/usr/bin/env bash
set -euo pipefail

# Start the local SQLite demo without taking ownership of any host-wide service.
# The only processes this script may stop are the API, worker, and legacy UI it records below.

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
default_data_dir="$root/.clerksan-demo"
runtime_dir="$root/.clerksan-runtime"
data_dir="$default_data_dir"
dry_run=false

readonly api_port=8000
readonly ollama_url="http://127.0.0.1:11434"
readonly launcher_intake_mode="legacy"
readonly database_file="clerksan.sqlite"
readonly storage_directory="doc_store"
readonly web_directory="$root/web"
readonly web_manifest="$web_directory/dist/.vite/manifest.json"
readonly requirements_lock="$root/requirements.lock"
readonly dependency_environments_dir="$runtime_dir/python-envs"
readonly dependency_stamp_name=".clerksan-lock-sha256"
readonly dependency_lock_snapshot_name=".clerksan-requirements.lock"
readonly sqlite_upgrade_state_root="$runtime_dir/sqlite-upgrade"

dependency_lock_digest=""
dependency_environment=""
dependency_python=""
dependency_setup_lock=""
dependency_setup_owner=""
dependency_temp_environment=""
dependency_lock_candidate=""

usage() {
  printf '%s\n' \
    "Usage: scripts/local-app.sh <command> [options]" \
    "" \
    "Commands:" \
    "  init-demo  Create an empty local SQLite demo dataset (never resets data)." \
    "  start      Build the local browser UI, then start the loopback API and worker." \
    "  stop       Stop only processes previously started by this launcher." \
    "  status     Show launcher process state and local prerequisites." \
    "" \
    "Options:" \
    "  --data-dir PATH  Use a different SQLite demo directory (default: .clerksan-demo)." \
    "  --dry-run        Print the data-changing commands without running them." \
    "  -h, --help       Show this help." \
    "" \
    "Examples:" \
    "  scripts/local-app.sh init-demo" \
    "  scripts/local-app.sh start" \
    "  scripts/local-app.sh status --data-dir .clerksan-ui-demo-20260810" \
    "  scripts/local-app.sh stop"
}

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

usage_error() {
  printf '%s\n' "$*" >&2
  usage >&2
  exit 2
}

show_command() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

require_command() {
  local command_name=$1
  command -v "$command_name" >/dev/null || die "Required command is unavailable: $command_name"
}

cleanup_dependency_setup() {
  if [[ -n "$dependency_temp_environment" && -d "$dependency_environments_dir" && \
    ! -L "$dependency_environments_dir" ]]; then
    case "$dependency_temp_environment" in
      "$dependency_environments_dir"/.tmp-"$dependency_lock_digest"-*)
        rm -rf -- "$dependency_temp_environment"
        ;;
    esac
  fi
  if [[ -n "$dependency_lock_candidate" ]]; then
    case "$dependency_lock_candidate" in
      "$runtime_dir"/.python-env-setup.owner.*)
        if [[ -f "$dependency_lock_candidate" && ! -L "$dependency_lock_candidate" ]]; then
          rm -f -- "$dependency_lock_candidate"
        fi
        ;;
    esac
  fi
  if [[ -n "$dependency_setup_lock" && "$dependency_setup_lock" == "$runtime_dir/python-env-setup.lock" && \
    -n "$dependency_setup_owner" ]] && read_dependency_lock_owner "$dependency_setup_lock"; then
    if [[ "$record_pid" == "$$" && "$record_start_token" == "$dependency_setup_owner" ]]; then
      case "$record_lock_format" in
        file) rm -f -- "$dependency_setup_lock" ;;
        legacy-directory)
          rm -f -- "$dependency_setup_lock/owner"
          rmdir -- "$dependency_setup_lock" 2>/dev/null || true
          ;;
      esac
    fi
  fi
}

trap cleanup_dependency_setup EXIT

sha256_file() {
  local path=$1
  local digest=""

  if command -v sha256sum >/dev/null; then
    digest="$(sha256sum -- "$path" | awk '{print $1}')"
  elif command -v shasum >/dev/null; then
    digest="$(shasum -a 256 -- "$path" | awk '{print $1}')"
  elif command -v openssl >/dev/null; then
    digest="$(openssl dgst -sha256 -- "$path" | awk '{print $NF}')"
  else
    die "A SHA-256 utility is required (sha256sum, shasum, or openssl)."
  fi
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || die "Could not calculate the dependency lock SHA-256."
  printf '%s\n' "$digest"
}

resolve_dependency_environment() {
  [[ -f "$requirements_lock" && ! -L "$requirements_lock" ]] || die \
    "Dependency lock is missing or unsafe: $requirements_lock"
  dependency_lock_digest="$(sha256_file "$requirements_lock")"
  dependency_environment="$dependency_environments_dir/$dependency_lock_digest"
  dependency_python="$dependency_environment/bin/python"
}

dependency_stamp_matches() {
  local environment=$1
  local expected=$2
  local stamp="$environment/$dependency_stamp_name"
  local recorded=""
  local extra=""

  [[ -f "$stamp" && ! -L "$stamp" ]] || return 1
  {
    IFS= read -r recorded
    IFS= read -r extra || true
  } <"$stamp"
  [[ "$recorded" == "$expected" && -z "$extra" ]]
}

dependency_environment_is_ready() {
  local snapshot="$dependency_environment/$dependency_lock_snapshot_name"

  [[ -d "$dependency_environment" && ! -L "$dependency_environment" && \
    -x "$dependency_python" && -f "$snapshot" && ! -L "$snapshot" ]] || return 1
  dependency_stamp_matches "$dependency_environment" "$dependency_lock_digest" && \
    [[ "$(sha256_file "$snapshot")" == "$dependency_lock_digest" ]]
}

ensure_supported_intake_mode() {
  local requested_mode="${CLERKSAN_INTAKE_MODE:-$launcher_intake_mode}"

  case "$requested_mode" in
    legacy) return 0 ;;
    universal) die "sandbox_unavailable" ;;
    *) die "Invalid CLERKSAN_INTAKE_MODE; expected legacy or universal." ;;
  esac
}

resolve_data_dir() {
  local requested=$1

  if [[ "$requested" != /* ]]; then
    requested="$PWD/$requested"
  fi

  if [[ -e "$requested" ]]; then
    [[ -d "$requested" && ! -L "$requested" ]] || die "Data directory must be a regular directory: $requested"
    data_dir="$(CDPATH= cd -- "$requested" && pwd -P)"
  else
    data_dir="$requested"
  fi
}

directory_is_empty() {
  local directory=$1
  local existing_entry

  existing_entry="$(find "$directory" -mindepth 1 -maxdepth 1 -print -quit)"
  [[ -z "$existing_entry" ]]
}

ensure_empty_demo_target() {
  if [[ -e "$data_dir" ]]; then
    [[ -d "$data_dir" && ! -L "$data_dir" ]] || die "Demo destination must be a regular directory: $data_dir"
    directory_is_empty "$data_dir" || die "Demo destination is not empty: $data_dir"
  fi
}

ensure_data_dir_ready() {
  [[ -d "$data_dir" && ! -L "$data_dir" ]] || die \
    "Demo data directory is unavailable: $data_dir"$'\n'"Run: scripts/local-app.sh init-demo --data-dir '$data_dir'"
  [[ -f "$data_dir/$database_file" && ! -L "$data_dir/$database_file" ]] || die \
    "Demo database is missing or unsafe: $data_dir/$database_file"$'\n'"Run: scripts/local-app.sh init-demo --data-dir '$data_dir'"
  [[ -d "$data_dir/$storage_directory" && ! -L "$data_dir/$storage_directory" ]] || die \
    "Demo document store is missing: $data_dir/$storage_directory"$'\n'"Run: scripts/local-app.sh init-demo --data-dir '$data_dir'"
}

runtime_dir_is_safe() {
  if [[ -e "$runtime_dir" || -L "$runtime_dir" ]]; then
    [[ -d "$runtime_dir" && ! -L "$runtime_dir" ]]
    return
  fi
  return 0
}

ensure_runtime_dir() {
  runtime_dir_is_safe || die "Launcher runtime path must be a regular directory: $runtime_dir"
  if [[ ! -e "$runtime_dir" ]]; then
    mkdir -p "$runtime_dir"
  fi
}

pid_file_for() {
  printf '%s/%s.pid\n' "$runtime_dir" "$1"
}

log_file_for() {
  printf '%s/%s.log\n' "$runtime_dir" "$1"
}

role_signature() {
  case "$1" in
    api) printf '%s\n' "uvicorn clerksan.api.main:app" ;;
    worker) printf '%s\n' "clerksan.ingest.worker" ;;
    ui) printf '%s\n' "streamlit run app.py" ;;
    *) die "Unknown launcher role: $1" ;;
  esac
}

role_label() {
  case "$1" in
    api) printf '%s\n' "API" ;;
    worker) printf '%s\n' "Worker" ;;
    ui) printf '%s\n' "UI" ;;
    *) die "Unknown launcher role: $1" ;;
  esac
}

ensure_web_build() {
  [[ -f "$web_directory/package.json" && -f "$web_directory/package-lock.json" ]] || die \
    "Local browser UI dependencies are missing under $web_directory. Restore the React workspace before starting."
  require_command npm
  if [[ "$dry_run" == true ]]; then
    printf '%s\n' "Would run a locked UI build and verify $web_manifest:"
    show_command npm --prefix "$web_directory" ci
    show_command npm --prefix "$web_directory" run build
    return 0
  fi
  (
    cd "$root"
    npm --prefix "$web_directory" ci
    npm --prefix "$web_directory" run build
  ) || die "The local browser UI build failed. No API or worker process was started."
  [[ -s "$web_manifest" ]] || die \
    "The local browser UI build did not produce the required Vite manifest: $web_manifest"
}

process_start_token() {
  ps -p "$1" -o lstart= 2>/dev/null | awk '{$1=$1; print}'
}

process_command() {
  ps -ww -p "$1" -o command= 2>/dev/null | awk '{$1=$1; print}'
}

record_pid=""
record_start_token=""
record_data_dir=""
role_state_value=""

read_pid_record() {
  local file=$1
  local extra=""

  record_pid=""
  record_start_token=""
  record_data_dir=""
  [[ -f "$file" && ! -L "$file" ]] || return 1
  {
    IFS= read -r record_pid
    IFS= read -r record_start_token
    IFS= read -r record_data_dir
    IFS= read -r extra || true
  } <"$file"
  [[ "$record_pid" =~ ^[1-9][0-9]*$ && -n "$record_start_token" && \
    "$record_data_dir" == /* && -z "$extra" ]]
}

role_state() {
  local role=$1
  local file
  local current_start_token
  local command_line
  local signature

  record_pid=""
  record_start_token=""
  record_data_dir=""
  role_state_value=""
  file="$(pid_file_for "$role")"
  if [[ ! -e "$file" ]]; then
    role_state_value="absent"
    return 0
  fi
  if [[ -L "$file" || ! -f "$file" ]] || ! read_pid_record "$file"; then
    role_state_value="invalid-record"
    return 0
  fi
  if ! kill -0 "$record_pid" 2>/dev/null; then
    role_state_value="stale"
    return 0
  fi
  current_start_token="$(process_start_token "$record_pid")"
  if [[ -z "$current_start_token" || "$current_start_token" != "$record_start_token" ]]; then
    role_state_value="ownership-mismatch"
    return 0
  fi
  command_line="$(process_command "$record_pid")"
  signature="$(role_signature "$role")"
  if [[ "$command_line" != *"$signature"* ]]; then
    role_state_value="ownership-mismatch"
    return 0
  fi
  role_state_value="running"
}

ensure_no_launcher_roles_running_for_dependency_setup() {
  local role

  for role in api worker ui; do
    role_state "$role"
    case "$role_state_value" in
      absent|stale) ;;
      running)
        die "Cannot provision launcher dependencies while $(role_label "$role") is running. Stop Clerk-san, then retry."
        ;;
      invalid-record|ownership-mismatch)
        die "Cannot provision launcher dependencies while $(role_label "$role") has an unsafe launcher record."
        ;;
      *) die "Unknown $(role_label "$role") launcher state: $role_state_value" ;;
    esac
  done
}

dependency_environments_dir_is_safe() {
  if [[ -e "$dependency_environments_dir" || -L "$dependency_environments_dir" ]]; then
    [[ -d "$dependency_environments_dir" && ! -L "$dependency_environments_dir" ]]
    return
  fi
  return 0
}

ensure_dependency_environments_dir() {
  dependency_environments_dir_is_safe || die \
    "Launcher dependency path must be a regular directory: $dependency_environments_dir"
  if [[ -e "$dependency_environments_dir" ]]; then
    return 0
  fi
  mkdir -p -- "$dependency_environments_dir"
}

record_lock_format=""

read_dependency_lock_owner() {
  local lock=$1
  local owner_file=""
  local expected_marker=""
  local marker=""
  local owner_pid=""
  local owner_start_token=""
  local extra=""
  local entry_count=""

  if [[ -f "$lock" && ! -L "$lock" ]]; then
    owner_file="$lock"
    expected_marker="clerksan-local-app-v2"
    record_lock_format="file"
  elif [[ -d "$lock" && ! -L "$lock" ]]; then
    owner_file="$lock/owner"
    expected_marker="clerksan-local-app-v1"
    record_lock_format="legacy-directory"
    [[ -f "$owner_file" && ! -L "$owner_file" ]] || return 1
    entry_count="$(find "$lock" -mindepth 1 -maxdepth 1 -print | wc -l | tr -d '[:space:]')"
    [[ "$entry_count" == "1" ]] || return 1
  else
    return 1
  fi
  {
    IFS= read -r marker
    IFS= read -r owner_pid
    IFS= read -r owner_start_token
    IFS= read -r extra || true
  } <"$owner_file"
  [[ "$marker" == "$expected_marker" && "$owner_pid" =~ ^[1-9][0-9]*$ && \
    -n "$owner_start_token" && -z "$extra" ]] || return 1
  record_pid="$owner_pid"
  record_start_token="$owner_start_token"
}

dependency_lock_owner_is_active() {
  local lock=$1
  local current_start_token=""

  read_dependency_lock_owner "$lock" || return 2
  kill -0 "$record_pid" 2>/dev/null || return 1
  current_start_token="$(process_start_token "$record_pid")"
  [[ -n "$current_start_token" && "$current_start_token" == "$record_start_token" ]]
}

dependency_lock_quarantine=""

quarantine_dependency_lock() {
  local lock=$1
  local classification=$2
  local quarantine=""
  local quarantine_directory=""

  dependency_lock_quarantine=""
  if [[ -f "$lock" && ! -L "$lock" ]]; then
    quarantine="$(mktemp "$runtime_dir/python-env-setup.lock.${classification}-XXXXXX")" || die \
      "Could not create a safe dependency-lock quarantine."
    if ! mv -f -- "$lock" "$quarantine"; then
      rm -f -- "$quarantine"
      die "The launcher dependency setup lock could not be quarantined safely."
    fi
  elif [[ -d "$lock" && ! -L "$lock" ]]; then
    quarantine_directory="$(mktemp -d "$runtime_dir/python-env-setup.lock.${classification}-XXXXXX")" || die \
      "Could not create a safe dependency-lock quarantine."
    quarantine="$quarantine_directory/lock"
    if ! mv -- "$lock" "$quarantine"; then
      rmdir -- "$quarantine_directory" 2>/dev/null || true
      die "The launcher dependency setup lock could not be quarantined safely."
    fi
  else
    die "The launcher dependency setup lock is unsafe: $lock"
  fi
  dependency_lock_quarantine="$quarantine"
}

reclaim_stale_dependency_lock() {
  local lock=$1
  local stale_pid=""
  local stale_start_token=""
  local stale_format=""
  local quarantine=""
  local quarantine_parent=""

  [[ ! -L "$lock" && ( -f "$lock" || -d "$lock" ) ]] || die \
    "The launcher dependency setup lock is malformed or unsafe: $lock"
  if dependency_lock_owner_is_active "$lock"; then
    die "Another launcher start or dependency setup is in progress. Retry after it finishes."
  else
    case "$?" in
      1)
        stale_pid="$record_pid"
        stale_start_token="$record_start_token"
        stale_format="$record_lock_format"
        ;;
      2)
        quarantine_dependency_lock "$lock" "abandoned"
        printf '%s\n' "Quarantined an abandoned dependency setup lock at $dependency_lock_quarantine." >&2
        return 0
        ;;
      *) die "The launcher dependency setup lock has an unknown state: $lock" ;;
    esac
  fi
  quarantine_dependency_lock "$lock" "stale"
  quarantine="$dependency_lock_quarantine"
  if ! read_dependency_lock_owner "$quarantine" || [[ "$record_pid" != "$stale_pid" || \
    "$record_start_token" != "$stale_start_token" || "$record_lock_format" != "$stale_format" ]]; then
    if [[ ! -e "$lock" && ! -L "$lock" ]]; then
      mv -- "$quarantine" "$lock" 2>/dev/null || true
    fi
    die "The stale launcher dependency setup lock changed during recovery."
  fi
  case "$record_lock_format" in
    file) rm -f -- "$quarantine" ;;
    legacy-directory)
      quarantine_parent="${quarantine%/lock}"
      rm -f -- "$quarantine/owner"
      rmdir -- "$quarantine"
      rmdir -- "$quarantine_parent"
      ;;
    *) die "The stale launcher dependency setup lock has an unknown format." ;;
  esac
}

acquire_dependency_setup_lock() {
  local lock="$runtime_dir/python-env-setup.lock"
  local owner_start_token=""
  local temporary_owner=""
  local attempts=0

  owner_start_token="$(process_start_token "$$")"
  [[ -n "$owner_start_token" ]] || die "Could not establish launcher ownership for dependency setup."
  temporary_owner="$(mktemp "$runtime_dir/.python-env-setup.owner.XXXXXX")" || {
    die "Could not create the launcher dependency-lock owner record."
  }
  dependency_lock_candidate="$temporary_owner"
  if ! printf '%s\n%s\n%s\n' "clerksan-local-app-v2" "$$" "$owner_start_token" >"$temporary_owner"; then
    rm -f -- "$temporary_owner"
    dependency_lock_candidate=""
    die "Could not write the launcher dependency-lock owner record."
  fi
  chmod 600 "$temporary_owner" || die "Could not protect the launcher dependency-lock owner record."

  while ((attempts < 3)); do
    if [[ -e "$lock" || -L "$lock" ]]; then
      reclaim_stale_dependency_lock "$lock"
    fi
    if ln -- "$temporary_owner" "$lock" 2>/dev/null; then
      if [[ -f "$lock" && ! -L "$lock" && "$lock" -ef "$temporary_owner" ]]; then
        dependency_setup_lock="$lock"
        dependency_setup_owner="$owner_start_token"
        rm -f -- "$temporary_owner"
        dependency_lock_candidate=""
        return 0
      fi
    fi
    attempts=$((attempts + 1))
  done
  rm -f -- "$temporary_owner"
  dependency_lock_candidate=""
  die "Another launcher start or dependency setup began during lock acquisition."
}

prune_stale_dependency_environments() {
  local candidate
  local candidate_digest

  ensure_no_launcher_roles_running_for_dependency_setup
  for candidate in "$dependency_environments_dir"/*; do
    [[ -e "$candidate" || -L "$candidate" ]] || continue
    [[ "$candidate" != "$dependency_environment" ]] || continue
    candidate_digest="${candidate##*/}"
    [[ "$candidate_digest" =~ ^[0-9a-f]{64}$ ]] || continue
    [[ -d "$candidate" && ! -L "$candidate" && -x "$candidate/bin/python" ]] || continue
    dependency_stamp_matches "$candidate" "$candidate_digest" || continue
    rm -rf -- "$candidate"
  done
}

ensure_launcher_runtime() {
  local preview_environment
  local preview_snapshot
  local preview_python
  local snapshot
  local stamp

  runtime_dir_is_safe || die "Launcher runtime path must be a regular directory: $runtime_dir"
  dependency_environments_dir_is_safe || die \
    "Launcher dependency path must be a regular directory: $dependency_environments_dir"
  resolve_dependency_environment
  preview_environment="$dependency_environments_dir/.tmp-$dependency_lock_digest-setup"
  preview_snapshot="$preview_environment/$dependency_lock_snapshot_name"
  preview_python="$preview_environment/bin/python"
  if [[ "$dry_run" == true ]]; then
    if ! dependency_environment_is_ready; then
      printf '%s\n' "Would provision a launcher-owned Python environment from the hash lock:"
      show_command uv venv --python 3.11 "$preview_environment"
      show_command cp -- "$requirements_lock" "$preview_snapshot"
      show_command uv pip install --python "$preview_python" --require-hashes --no-deps \
        --requirement "$preview_snapshot"
    fi
    return 0
  fi

  ensure_runtime_dir
  ensure_dependency_environments_dir
  acquire_dependency_setup_lock
  if dependency_environment_is_ready; then
    return 0
  fi

  require_command uv
  ensure_no_launcher_roles_running_for_dependency_setup
  if [[ -e "$dependency_environment" || -L "$dependency_environment" ]]; then
    die "Launcher dependency environment exists without a valid owner stamp: $dependency_environment"
  fi

  dependency_temp_environment="$dependency_environments_dir/.tmp-$dependency_lock_digest-$$"
  [[ ! -e "$dependency_temp_environment" && ! -L "$dependency_temp_environment" ]] || die \
    "Launcher dependency staging path already exists; inspect the runtime directory before retrying."
  uv venv --python 3.11 "$dependency_temp_environment" || die \
    "Could not create the launcher-owned Python environment."
  snapshot="$dependency_temp_environment/$dependency_lock_snapshot_name"
  cp -- "$requirements_lock" "$snapshot"
  [[ "$(sha256_file "$snapshot")" == "$dependency_lock_digest" ]] || die \
    "Dependency lock changed during setup; no launcher environment was published."
  uv pip install \
    --python "$dependency_temp_environment/bin/python" \
    --require-hashes \
    --no-deps \
    --requirement "$snapshot" || die \
    "Dependency installation failed; requirements.lock hashes were enforced."

  stamp="$dependency_temp_environment/$dependency_stamp_name"
  printf '%s\n' "$dependency_lock_digest" >"$stamp"
  [[ ! -e "$dependency_environment" && ! -L "$dependency_environment" ]] || die \
    "Launcher dependency environment changed during setup; refusing to replace it."
  mv -- "$dependency_temp_environment" "$dependency_environment"
  dependency_temp_environment=""
  dependency_python="$dependency_environment/bin/python"
  dependency_environment_is_ready || die \
    "Launcher dependency environment did not validate after installation."

  prune_stale_dependency_environments
}

remove_stale_record() {
  local role=$1
  local file
  file="$(pid_file_for "$role")"
  [[ -f "$file" && ! -L "$file" ]] || return 0
  rm -f -- "$file"
}

write_pid_record() {
  local role=$1
  local pid=$2
  local file
  local temporary_file
  local start_token=""
  local attempt=0

  file="$(pid_file_for "$role")"
  if [[ -L "$file" || ( -e "$file" && ! -f "$file" ) ]]; then
    return 1
  fi
  temporary_file="$(mktemp "$runtime_dir/.${role}.pid.XXXXXX")" || return 1
  while ((attempt < 20)); do
    start_token="$(process_start_token "$pid")"
    if [[ -n "$start_token" ]]; then
      break
    fi
    attempt=$((attempt + 1))
    sleep 0.05
  done
  if [[ -z "$start_token" ]]; then
    rm -f -- "$temporary_file"
    return 1
  fi
  if ! printf '%s\n%s\n%s\n' "$pid" "$start_token" "$data_dir" >"$temporary_file"; then
    rm -f -- "$temporary_file"
    return 1
  fi
  if ! mv -f -- "$temporary_file" "$file"; then
    rm -f -- "$temporary_file"
    return 1
  fi
}

show_role_log() {
  local role=$1
  local file
  file="$(log_file_for "$role")"
  if [[ -f "$file" && ! -L "$file" ]]; then
    printf '%s\n' "Last lines from $file:" >&2
    tail -n 30 "$file" >&2 || true
  fi
}

port_listener_pid() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true
}

ensure_port_is_free() {
  local port=$1
  local label=$2
  local listener

  listener="$(port_listener_pid "$port")"
  [[ -z "$listener" ]] || die \
    "$label cannot start because 127.0.0.1:$port is already in use by PID $listener."$'\n'"The launcher will not stop an unknown process; free that port or run status first."
}

ollama_models=""
processing_models_ready=false
required_models=()

load_required_models() {
  local output=""
  local model=""

  [[ -n "$dependency_python" && -x "$dependency_python" ]] || return 1
  output="$(
    cd "$root"
    env \
      "CLERKSAN_INTAKE_MODE=$launcher_intake_mode" \
      "CLERKSAN_DATABASE_URL=$(database_url)" \
      "CLERKSAN_STORAGE_DIR=$data_dir/$storage_directory" \
      "CLERKSAN_DEMO_MODE=false" \
      "CLERKSAN_OLLAMA_URL=$ollama_url" \
      "$dependency_python" -m clerksan.tools.local_preview required-models
  )" || return 1
  required_models=()
  while IFS= read -r model; do
    [[ -n "$model" ]] || continue
    required_models+=("$model")
  done <<<"$output"
  ((${#required_models[@]} > 0))
}

fetch_ollama_models() {
  ollama_models="$(
    OLLAMA_HOST=127.0.0.1:11434 "$dependency_python" -c \
      'import os, subprocess, sys
try:
    result = subprocess.run(
        ["ollama", "list"],
        check=False,
        env=os.environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
    )
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit(1)
if result.returncode != 0:
    raise SystemExit(1)
sys.stdout.write(result.stdout)'
  )" || return 1
}

ollama_has_model() {
  local model=$1
  local canonical_model="${model%:latest}"

  printf '%s\n' "$ollama_models" | awk -v expected="$canonical_model" '
    NR > 1 {
      candidate = $1
      sub(/:latest$/, "", candidate)
      if (candidate == expected) found = 1
    }
    END { exit(found ? 0 : 1) }
  '
}

ensure_ollama_ready() {
  local model
  local missing=()

  if ! curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
    "$ollama_url/api/tags" >/dev/null; then
    die "Ollama is not reachable at $ollama_url. Start your existing Ollama service, then retry."
  fi
  if ! load_required_models; then
    die "Configured model requirements could not be evaluated safely."
  fi
  if ! fetch_ollama_models; then
    die "Ollama is reachable, but its CLI could not list local models. Check: ollama list"
  fi
  for model in "${required_models[@]}"; do
    if ! ollama_has_model "$model"; then
      missing+=("$model")
    fi
  done
  if ((${#missing[@]} > 0)); then
    printf '%s\n' "Required Ollama models are missing:" >&2
    for model in "${missing[@]}"; do
      printf '%s\n' "  ollama pull $model" >&2
    done
    exit 1
  fi
}

prepare_existing_roles_for_start() {
  local role
  local state
  local label

  for role in api worker ui; do
    role_state "$role"
    state="$role_state_value"
    label="$(role_label "$role")"
    case "$state" in
      absent|running) ;;
      stale)
        printf '%s\n' "Removing stale $label launcher record."
        remove_stale_record "$role"
        ;;
      invalid-record|ownership-mismatch)
        die "$label has an unsafe launcher record at $(pid_file_for "$role")."$'\n'"Refusing to touch it. Inspect the process and record manually, then retry."
        ;;
      *) die "Unknown $label launcher state: $state" ;;
    esac
  done
}

retire_legacy_ui_for_start() {
  local state

  role_state ui
  state="$role_state_value"
  case "$state" in
    absent) return 0 ;;
    running)
      [[ "$record_data_dir" == "$data_dir" ]] || die \
        "Legacy Streamlit UI is still running for a different data directory: $record_data_dir"$'\n'"Stop it explicitly before starting the React UI."
      stop_role ui
      ;;
    stale)
      remove_stale_record ui
      ;;
    invalid-record|ownership-mismatch)
      die "Legacy Streamlit UI has an unsafe launcher record at $(pid_file_for ui)."
      ;;
    *) die "Unknown legacy UI launcher state: $state" ;;
  esac
}

database_url() {
  printf 'sqlite+aiosqlite:///%s/%s\n' "$data_dir" "$database_file"
}

start_role() {
  local role=$1
  local database_url_value
  local storage_path
  local log_file
  local temporary_log
  local pid
  local -a command

  database_url_value="$(database_url)"
  storage_path="$data_dir/$storage_directory"
  log_file="$(log_file_for "$role")"
  case "$role" in
    api)
      command=(
        env
        "CLERKSAN_INTAKE_MODE=$launcher_intake_mode"
        "CLERKSAN_DATABASE_URL=$database_url_value"
        "CLERKSAN_STORAGE_DIR=$storage_path"
        "CLERKSAN_DEMO_MODE=false"
        "CLERKSAN_OLLAMA_URL=$ollama_url"
        "CLERKSAN_UI_STATIC_DIR=$web_directory/dist"
        "$dependency_python" -m uvicorn clerksan.api.main:app
        --host 127.0.0.1 --port "$api_port"
      )
      ;;
    worker)
      command=(
        env
        "CLERKSAN_INTAKE_MODE=$launcher_intake_mode"
        "CLERKSAN_DATABASE_URL=$database_url_value"
        "CLERKSAN_STORAGE_DIR=$storage_path"
        "CLERKSAN_DEMO_MODE=false"
        "CLERKSAN_OLLAMA_URL=$ollama_url"
        "$dependency_python" -m clerksan.ingest.worker
      )
      ;;
    ui) die "The legacy Streamlit UI can only be stopped through its existing launcher record." ;;
    *) die "Unknown launcher role: $role" ;;
  esac

  if [[ "$dry_run" == true ]]; then
    printf '%s\n' "Would launch $(role_label "$role") and write its log to $log_file:"
    show_command "${command[@]}"
    return 0
  fi

  if [[ -L "$log_file" || ( -e "$log_file" && ! -f "$log_file" ) ]]; then
    printf '%s\n' "$(role_label "$role") log path is unsafe: $log_file" >&2
    return 1
  fi
  temporary_log="$(mktemp "$runtime_dir/.${role}.log.XXXXXX")" || {
    printf '%s\n' "Could not create the $(role_label "$role") log safely." >&2
    return 1
  }
  exec 9>"$temporary_log"
  if ! mv -f -- "$temporary_log" "$log_file"; then
    exec 9>&-
    rm -f -- "$temporary_log"
    printf '%s\n' "Could not publish the $(role_label "$role") log safely." >&2
    return 1
  fi

  (
    cd "$root"
    # Protect the long-lived Python process group from the invoking terminal's
    # hangup so the PID record stays valid after this launcher returns.
    exec nohup "${command[@]}"
  ) >&9 2>&1 &
  pid=$!
  exec 9>&-
  if ! write_pid_record "$role" "$pid"; then
    kill -TERM "$pid" 2>/dev/null || true
    local attempts=0
    while kill -0 "$pid" 2>/dev/null && ((attempts < 40)); do
      attempts=$((attempts + 1))
      sleep 0.05
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
    printf '%s\n' "Could not safely record the $(role_label "$role") process ID." >&2
    return 1
  fi
  printf '%s\n' "Started $(role_label "$role") (PID $pid)."
}

readiness_payload=""

fetch_readiness() {
  readiness_payload="$(
    curl --silent --show-error --connect-timeout 1 --max-time 2 \
      "http://127.0.0.1:$api_port/ready"
  )" || return 1
  [[ -n "$readiness_payload" ]]
}

inspect_readiness() {
  local mode=$1
  printf '%s' "$readiness_payload" | (
    cd "$root"
    "$dependency_python" -m clerksan.tools.local_preview readiness "$mode"
  )
}

wait_for_core_readiness() {
  local attempts=0
  local state
  local message

  while ((attempts < 90)); do
    if fetch_readiness 2>/dev/null && message="$(inspect_readiness core 2>/dev/null)"; then
      printf '%s\n' "$message"
      return 0
    fi
    role_state api
    state="$role_state_value"
    if [[ "$state" != "running" ]]; then
      printf '%s\n' "API stopped before core readiness (state: $state)." >&2
      show_role_log api
      return 1
    fi
    attempts=$((attempts + 1))
    sleep 0.5
  done
  printf '%s\n' "API did not establish normal-mode core readiness in time." >&2
  show_role_log api
  return 1
}

wait_for_worker() {
  local attempts=0
  local state

  while ((attempts < 4)); do
    sleep 0.5
    role_state worker
    state="$role_state_value"
    if [[ "$state" == "running" ]]; then
      return 0
    fi
    attempts=$((attempts + 1))
  done
  printf '%s\n' "Worker stopped during startup (state: $state)." >&2
  show_role_log worker
  return 1
}

wait_for_processing_state() {
  local attempts=0
  local code
  local message

  while ((attempts < 60)); do
    if ! fetch_readiness 2>/dev/null; then
      attempts=$((attempts + 1))
      sleep 0.5
      continue
    fi
    if message="$(inspect_readiness processing 2>/dev/null)"; then
      printf '%s\n' "$message"
      return 0
    else
      code=$?
    fi
    case "$code" in
      3)
        printf '%s\n' "$message"
        return 0
        ;;
      4) ;;
      *)
        printf '%s\n' "Core readiness was lost while waiting for processing evidence." >&2
        return 1
        ;;
    esac
    attempts=$((attempts + 1))
    sleep 0.5
  done

  if fetch_readiness 2>/dev/null; then
    if message="$(inspect_readiness processing 2>/dev/null)"; then
      printf '%s\n' "$message"
      return 0
    else
      code=$?
      if [[ "$code" == 3 || "$code" == 4 ]]; then
        printf '%s\n' "$message"
        return 0
      fi
    fi
  fi
  printf '%s\n' "Processing: unavailable (fresh worker/model evidence was not established)." >&2
  return 1
}

prepare_sqlite_upgrade_rollback() {
  local state

  role_state worker
  state="$role_state_value"
  if [[ "$state" == "running" ]]; then
    die "Worker is running while the API needs a new schema check."$'\n'\
"Run scripts/local-app.sh stop before restarting this data directory."
  fi
  (
    cd "$root"
    "$dependency_python" -m clerksan.tools.local_preview prepare-sqlite-upgrade \
      --database "$data_dir/$database_file" \
      --storage "$data_dir/$storage_directory" \
      --state-root "$sqlite_upgrade_state_root"
  ) || die "A verified SQLite rollback snapshot could not be prepared; the API was not started."
}

mark_sqlite_upgrade_ready() {
  (
    cd "$root"
    "$dependency_python" -m clerksan.tools.local_preview mark-sqlite-upgrade \
      --database "$data_dir/$database_file" \
      --state-root "$sqlite_upgrade_state_root"
  ) || return 1
}

started_roles=()

stop_role() {
  local role=$1
  local state
  local file
  local attempts=0
  local label
  local pid

  label="$(role_label "$role")"
  file="$(pid_file_for "$role")"
  role_state "$role"
  state="$role_state_value"
  case "$state" in
    absent)
      printf '%s\n' "$label is not managed by this launcher."
      return 0
      ;;
    stale)
      printf '%s\n' "Removing stale $label launcher record."
      if [[ "$dry_run" != true ]]; then
        remove_stale_record "$role"
      fi
      return 0
      ;;
    invalid-record|ownership-mismatch)
      printf '%s\n' "$label has an unsafe launcher record at $file; refusing to signal it." >&2
      return 1
      ;;
    running) ;;
    *)
      printf '%s\n' "Unknown $label launcher state: $state" >&2
      return 1
      ;;
  esac

  if [[ "$dry_run" == true ]]; then
    printf '%s\n' "Would stop $label (PID $record_pid)."
    return 0
  fi

  pid="$record_pid"
  role_state "$role"
  if [[ "$role_state_value" != "running" || "$record_pid" != "$pid" ]]; then
    printf '%s\n' "$label ownership changed before shutdown; refusing to signal it." >&2
    return 1
  fi
  printf '%s\n' "Stopping $label (PID $pid)."
  kill -TERM "$pid" || return 1
  while ((attempts < 40)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      remove_stale_record "$role"
      printf '%s\n' "$label stopped."
      return 0
    fi
    role_state "$role"
    case "$role_state_value" in
      stale)
        remove_stale_record "$role"
        printf '%s\n' "$label stopped."
        return 0
        ;;
      ownership-mismatch)
        remove_stale_record "$role"
        printf '%s\n' "$label stopped; its former PID was reused after shutdown."
        return 0
        ;;
      invalid-record)
        printf '%s\n' "$label PID record changed during shutdown; refusing to remove it." >&2
        return 1
        ;;
      running) ;;
      absent)
        printf '%s\n' "$label stopped."
        return 0
        ;;
      *)
        printf '%s\n' "$label has an unknown shutdown state: $role_state_value" >&2
        return 1
        ;;
    esac
    attempts=$((attempts + 1))
    sleep 0.25
  done
  printf '%s\n' "$label did not exit after a graceful stop; its PID record was kept at $file." >&2
  return 1
}

cleanup_started_roles() {
  local index

  for ((index=${#started_roles[@]} - 1; index >= 0; index--)); do
    stop_role "${started_roles[$index]}" || true
  done
}

ensure_running_role_healthy() {
  local role=$1
  local message

  case "$role" in
    api) ;;
    ui) return 0 ;;
    worker) return 0 ;;
  esac
  if ! fetch_readiness || ! message="$(inspect_readiness core)"; then
    die "$(role_label "$role") is recorded as running but normal-mode core readiness is unavailable."$'\n'"Run: scripts/local-app.sh stop, inspect $(log_file_for "$role"), then retry start."
  fi
  printf '%s\n' "$message"
}

start_if_needed() {
  local role=$1
  local port=""
  local state

  role_state "$role"
  state="$role_state_value"
  if [[ "$state" == "running" ]]; then
    if [[ "$record_data_dir" != "$data_dir" ]]; then
      die "$(role_label "$role") is already running for a different data directory: $record_data_dir"$'\n'"Stop it first, then start with: --data-dir '$data_dir'"
    fi
    ensure_running_role_healthy "$role"
    printf '%s\n' "$(role_label "$role") is already running under this launcher."
    return 0
  fi
  case "$role" in
    api) port=$api_port ;;
  esac
  if [[ -n "$port" ]]; then
    ensure_port_is_free "$port" "$(role_label "$role")"
  fi
  if [[ "$role" == "api" ]]; then
    prepare_sqlite_upgrade_rollback
  fi
  start_role "$role" || return 1
  started_roles+=("$role")
  case "$role" in
    api)
      wait_for_core_readiness || return 1
      mark_sqlite_upgrade_ready || return 1
      ;;
    worker) wait_for_worker ;;
  esac
}

init_demo() {
  ensure_empty_demo_target
  ensure_launcher_runtime
  if [[ "$dry_run" == true ]]; then
    printf '%s\n' "Would create a fresh demo dataset at $data_dir without --reset:"
    show_command env \
      "CLERKSAN_INTAKE_MODE=$launcher_intake_mode" \
      "CLERKSAN_DEMO_MODE=false" \
      "$dependency_python" -m scripts.demo_local --out "$data_dir"
    return 0
  fi
  require_command curl
  ensure_ollama_ready
  (
    cd "$root"
    env \
      "CLERKSAN_INTAKE_MODE=$launcher_intake_mode" \
      "CLERKSAN_DEMO_MODE=false" \
      "$dependency_python" -m scripts.demo_local --out "$data_dir"
  )
}

start_app() {
  ensure_supported_intake_mode
  ensure_data_dir_ready
  ensure_launcher_runtime
  if [[ "$dry_run" == true ]]; then
    printf '%s\n' "Dry-run: would validate the locked Python environment, npm, curl, lsof, local Ollama, and ports before starting."
    ensure_web_build
    printf '%s\n' "Would prepare and verify a rollback snapshot before any SQLite schema upgrade:"
    show_command "$dependency_python" -m clerksan.tools.local_preview prepare-sqlite-upgrade \
      --database "$data_dir/$database_file" \
      --storage "$data_dir/$storage_directory" \
      --state-root "$sqlite_upgrade_state_root"
    for role in api worker; do
      start_role "$role"
    done
    return 0
  fi

  require_command curl
  require_command lsof
  printf '%s\n' "Intake mode: $launcher_intake_mode"
  report_ollama_status
  if [[ "$processing_models_ready" != true ]]; then
    printf '%s\n' \
      "Processing: delayed until the configured local models are available; durable intake will still start."
  fi
  ensure_web_build
  ensure_runtime_dir
  prepare_existing_roles_for_start
  retire_legacy_ui_for_start

  if ! start_if_needed api || ! start_if_needed worker; then
    printf '%s\n' "Startup failed; stopping only roles started during this attempt." >&2
    cleanup_started_roles
    exit 1
  fi
  if ! wait_for_processing_state; then
    printf '%s\n' "Startup lost core readiness; stopping only roles started during this attempt." >&2
    cleanup_started_roles
    exit 1
  fi
  printf '%s\n' "Clerk-san core is available at http://127.0.0.1:$api_port"
}

report_data_dir() {
  if [[ ! -e "$data_dir" ]]; then
    printf '%s\n' "Data: missing ($data_dir)"
  elif [[ ! -d "$data_dir" || -L "$data_dir" ]]; then
    printf '%s\n' "Data: unsafe path ($data_dir)"
  elif [[ -L "$data_dir/$database_file" || ( -e "$data_dir/$database_file" && ! -f "$data_dir/$database_file" ) ]]; then
    printf '%s\n' "Data: unsafe database path ($data_dir/$database_file)"
  elif [[ ! -f "$data_dir/$database_file" || ! -d "$data_dir/$storage_directory" || -L "$data_dir/$storage_directory" ]]; then
    printf '%s\n' "Data: incomplete ($data_dir)"
  else
    printf '%s\n' "Data: ready ($data_dir)"
  fi
}

report_role_status() {
  local role=$1
  local state
  local label
  local health_url=""

  label="$(role_label "$role")"
  role_state "$role"
  state="$role_state_value"
  case "$state" in
    running)
      case "$role" in
        api) health_url="http://127.0.0.1:$api_port/health" ;;
      esac
      if [[ -n "$health_url" ]] && command -v curl >/dev/null && \
        curl --fail --silent --connect-timeout 1 --max-time 2 "$health_url" >/dev/null; then
        printf '%s\n' "$label: running and healthy (PID $record_pid; data: $record_data_dir)"
      elif [[ -n "$health_url" ]]; then
        printf '%s\n' "$label: running, health endpoint unavailable (PID $record_pid; data: $record_data_dir)"
      else
        printf '%s\n' "$label: running (PID $record_pid; data: $record_data_dir)"
      fi
      ;;
    absent) printf '%s\n' "$label: not started by this launcher" ;;
    stale) printf '%s\n' "$label: stale launcher record" ;;
    invalid-record|ownership-mismatch)
      printf '%s\n' "$label: unsafe launcher record (will not be signaled)"
      ;;
    *) printf '%s\n' "$label: unknown state ($state)" ;;
  esac
}

report_ollama_status() {
  local model
  local missing=()

  processing_models_ready=false

  if ! command -v curl >/dev/null; then
    printf '%s\n' "Ollama: cannot check (curl is unavailable)"
    return 0
  fi
  if ! curl --fail --silent --connect-timeout 1 --max-time 2 "$ollama_url/api/tags" >/dev/null; then
    printf '%s\n' "Ollama: unavailable at $ollama_url"
    return 0
  fi
  if [[ -z "$dependency_python" ]]; then
    resolve_dependency_environment
  fi
  if ! dependency_environment_is_ready || ! load_required_models; then
    printf '%s\n' "Ollama: reachable, but configured model requirements could not be evaluated"
    return 0
  fi
  if ! command -v ollama >/dev/null || ! fetch_ollama_models; then
    printf '%s\n' "Ollama: reachable, but models could not be listed"
    return 0
  fi
  for model in "${required_models[@]}"; do
    if ! ollama_has_model "$model"; then
      missing+=("$model")
    fi
  done
  if ((${#missing[@]} == 0)); then
    processing_models_ready=true
    printf '%s\n' "Ollama: reachable with required models"
  else
    printf '%s\n' "Ollama: reachable; missing ${missing[*]}"
  fi
}

show_status() {
  local message

  report_data_dir
  if ! runtime_dir_is_safe; then
    printf '%s\n' "Launcher runtime: unsafe path ($runtime_dir)"
    report_ollama_status
    return 1
  fi
  report_role_status api
  report_role_status worker
  report_role_status ui
  report_ollama_status
  role_state api
  if [[ "$role_state_value" != "running" ]]; then
    return 0
  fi
  resolve_dependency_environment
  if ! dependency_environment_is_ready; then
    printf '%s\n' "Readiness: launcher dependency environment is unavailable"
    return 1
  fi
  if ! fetch_readiness; then
    printf '%s\n' "Readiness: API response is unavailable"
    return 1
  fi
  if message="$(inspect_readiness status)"; then
    printf '%s\n' "$message"
    return 0
  fi
  printf '%s\n' "$message"
  return 1
}

stop_app() {
  local failed=false
  local role

  runtime_dir_is_safe || die "Launcher runtime path must be a regular directory: $runtime_dir"
  for role in ui worker api; do
    if ! stop_role "$role"; then
      failed=true
    fi
  done
  [[ "$failed" == false ]]
}

if (($# == 0)); then
  usage
  exit 2
fi

command_name=$1
shift
case "$command_name" in
  -h|--help)
    usage
    exit 0
    ;;
  init-demo|start|stop|status) ;;
  *) usage_error "Unknown command: $command_name" ;;
esac

while (($# > 0)); do
  case "$1" in
    --data-dir)
      (($# >= 2)) || usage_error "--data-dir requires a path."
      resolve_data_dir "$2"
      shift 2
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) usage_error "Unknown option: $1" ;;
  esac
done

case "$command_name" in
  init-demo) init_demo ;;
  start) start_app ;;
  stop) stop_app ;;
  status) show_status ;;
esac
