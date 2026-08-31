from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOCAL_APP = ROOT / "scripts" / "local-app.sh"


def run_local_app(
    *arguments: str,
    cwd: Path,
    script: Path = LOCAL_APP,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def isolated_local_app(tmp_path: Path) -> Path:
    script = tmp_path / "scripts" / "local-app.sh"
    script.parent.mkdir()
    shutil.copy2(LOCAL_APP, script)
    shutil.copy2(ROOT / "requirements.lock", tmp_path / "requirements.lock")
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text('{"name":"test-web"}', encoding="utf-8")
    (web / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
    return script


def process_start_token(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
        check=True,
    )
    token = " ".join(result.stdout.split())
    assert token
    return token


def write_pid_record(runtime_dir: Path, *, pid: int, data_dir: Path) -> Path:
    runtime_dir.mkdir(exist_ok=True)
    record = runtime_dir / "api.pid"
    record.write_text(
        f"{pid}\n{process_start_token(pid)}\n{data_dir}\n",
        encoding="utf-8",
    )
    return record


def write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def write_successful_npm_stub(bin_dir: Path) -> None:
    write_executable(
        bin_dir / "npm",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'prefix=""\n'
        'if [[ "${1:-}" == "--prefix" ]]; then prefix=$2; fi\n'
        'mkdir -p "$prefix/dist/.vite"\n'
        "printf '{}' > \"$prefix/dist/.vite/manifest.json\"\n",
    )


def write_long_running_launcher_python(root: Path) -> Path:
    lock = root / "requirements.lock"
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    environment = root / ".clerksan-runtime" / "python-envs" / digest
    bin_dir = environment / "bin"
    bin_dir.mkdir(parents=True)
    (environment / ".clerksan-lock-sha256").write_text(f"{digest}\n", encoding="ascii")
    (environment / ".clerksan-requirements.lock").write_bytes(lock.read_bytes())
    python = shlex.quote(sys.executable)
    source_root = shlex.quote(str(ROOT))
    write_executable(
        bin_dir / "python",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ -n "${CLERKSAN_TEST_EXPECTED_SETUP_LOCK:-}" && '
        '! -f "$CLERKSAN_TEST_EXPECTED_SETUP_LOCK" ]]; then exit 98; fi\n'
        "if [[ \"${1:-} ${2:-}\" == '-m clerksan.tools.local_preview' ]]; then\n"
        f"  export PYTHONPATH={source_root}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        f'  exec {python} "$@"\n'
        "fi\n"
        'if [[ "${1:-}" == "-c" ]]; then\n'
        f'  exec {python} "$@"\n'
        "fi\n"
        'if [[ "$*" == *"uvicorn clerksan.api.main:app"* ]]; then\n'
        f"  exec {python} -c 'import time; time.sleep(60)' "
        "'uvicorn clerksan.api.main:app'\n"
        "fi\n"
        'if [[ "$*" == *"clerksan.ingest.worker"* ]]; then\n'
        f"  exec {python} -c 'import time; time.sleep(60)' 'clerksan.ingest.worker'\n"
        "fi\n"
        "exit 99\n",
    )
    return environment


def write_provisioning_uv_stub(bin_dir: Path) -> None:
    python = shlex.quote(sys.executable)
    source_root = shlex.quote(str(ROOT))
    write_executable(
        bin_dir / "uv",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf \'%s\\n\' "$*" >> "$CLERKSAN_TEST_UV_LOG"\n'
        'if [[ "${1:-}" == "venv" ]]; then\n'
        "  environment=${!#}\n"
        '  mkdir -p "$environment/bin"\n'
        "  printf '%s\\n' '#!/usr/bin/env bash' 'set -euo pipefail' "
        '\'if [[ "${1:-} ${2:-}" == "-m clerksan.tools.local_preview" ]]; then\' '
        f"'  export PYTHONPATH={source_root}${{PYTHONPATH:+:$PYTHONPATH}}' "
        f"'  exec {python} \"$@\"' 'fi' "
        '\'if [[ "${1:-} ${2:-}" == "-m scripts.demo_local" ]]; then\' '
        '\'  [[ "${CLERKSAN_INTAKE_MODE:-}" == "legacy" ]]\' '
        '\'  [[ "${CLERKSAN_DEMO_MODE:-}" == "false" ]]\' '
        "'  exit 0' 'fi' "
        '\'if [[ "${1:-}" == "-c" ]]; then\' '
        f"'  exec {python} \"$@\"' 'fi' 'exit 0' > \"$environment/bin/python\"\n"
        '  chmod 755 "$environment/bin/python"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "${1:-} ${2:-}" == "pip install" ]]; then\n'
        '  if [[ -n "${CLERKSAN_TEST_MUTATE_LOCK:-}" ]]; then\n'
        "    printf '%s\\n' '# synthetic concurrent change' >> \"$CLERKSAN_TEST_MUTATE_LOCK\"\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "exit 99\n",
    )


def write_launcher_runtime_stubs(
    tmp_path: Path,
    *,
    models: tuple[str, ...] = (
        "gemma3:4b",
        "qwen2.5:7b",
        "nomic-embed-text:v1.5",
    ),
) -> tuple[Path, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    readiness = json.dumps(
        {
            "status": "ready",
            "demo_mode": False,
            "intake_ready": True,
            "review_ready": True,
            "processing_ready": True,
            "processing_reason_codes": [],
            "worker_registry_digest": "a" * 64,
            "worker_capabilities_digest": "b" * 64,
            "worker_capability_lease_age_seconds": 0.1,
        }
    )
    write_executable(
        bin_dir / "curl",
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"/ready"* ]]; then\n'
        f"  printf '%s\\n' {shlex.quote(readiness)}\n"
        "fi\n"
        "exit 0\n",
    )
    model_rows = "".join(f"printf '%s\\n' {shlex.quote(f'{model} x x x')}\n" for model in models)
    write_executable(
        bin_dir / "ollama",
        f"#!/usr/bin/env bash\nprintf '%s\\n' 'NAME ID SIZE MODIFIED'\n{model_rows}",
    )
    write_executable(bin_dir / "lsof", "#!/usr/bin/env bash\nexit 0\n")
    write_successful_npm_stub(bin_dir)
    write_long_running_launcher_python(tmp_path)
    return bin_dir, os.environ | {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def test_local_launcher_help_describes_safe_local_commands(tmp_path: Path) -> None:
    result = run_local_app("--help", cwd=tmp_path, script=isolated_local_app(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "init-demo" in result.stdout
    assert "start" in result.stdout
    assert "stop" in result.stdout
    assert "status" in result.stdout
    assert "--data-dir PATH" in result.stdout


def test_launcher_source_uses_only_the_hash_enforced_runtime_contract() -> None:
    source = LOCAL_APP.read_text(encoding="utf-8")

    assert "uv run" not in source
    assert "with-requirements" not in source
    assert "uv pip install" in source
    assert "--require-hashes" in source
    assert "--no-deps" in source
    assert '"$dependency_python" -m uvicorn' in source
    assert '"$dependency_python" -m clerksan.ingest.worker' in source
    assert '"$dependency_python" -m scripts.demo_local' in source
    assert source.count('"CLERKSAN_DEMO_MODE=false"') == 5
    assert "CLERKSAN_DEMO_MODE=true" not in source


def test_demo_module_entrypoint_resolves_project_imports() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.demo_local", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Run one real local receipt" in result.stdout


def test_init_demo_dry_run_does_not_create_or_reset_data(tmp_path: Path) -> None:
    destination = tmp_path / "fresh-demo"
    script = isolated_local_app(tmp_path)

    result = run_local_app(
        "init-demo",
        "--data-dir",
        str(destination),
        "--dry-run",
        cwd=tmp_path,
        script=script,
    )

    assert result.returncode == 0, result.stderr
    assert not destination.exists()
    assert "uv pip install" in result.stdout
    assert "--require-hashes" in result.stdout
    assert "--no-deps" in result.stdout
    launch_line = next(line for line in result.stdout.splitlines() if "scripts.demo_local" in line)
    assert "--reset" not in launch_line
    assert "uv run" not in launch_line
    assert ".clerksan-runtime/python-envs/" in launch_line


def test_init_demo_refuses_to_replace_nonempty_data_without_running_uv(tmp_path: Path) -> None:
    destination = tmp_path / "existing-demo"
    script = isolated_local_app(tmp_path)
    destination.mkdir()
    evidence = destination / "evidence.txt"
    evidence.write_text("preserve", encoding="utf-8")

    result = run_local_app(
        "init-demo",
        "--data-dir",
        str(destination),
        "--dry-run",
        cwd=tmp_path,
        script=script,
    )

    assert result.returncode != 0
    assert "not empty" in result.stderr
    assert evidence.read_text(encoding="utf-8") == "preserve"


def test_start_dry_run_requires_complete_data_but_never_probes_host_services(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "demo-data"
    script = isolated_local_app(tmp_path)
    data_dir.mkdir()
    (data_dir / "clerksan.sqlite").touch()
    (data_dir / "doc_store").mkdir()

    result = run_local_app(
        "start",
        "--data-dir",
        str(data_dir),
        "--dry-run",
        cwd=tmp_path,
        script=script,
    )

    assert result.returncode == 0, result.stderr
    assert "uvicorn clerksan.api.main:app" in result.stdout
    assert "clerksan.ingest.worker" in result.stdout
    assert result.stdout.count("CLERKSAN_INTAKE_MODE=legacy") == 2
    assert "npm --prefix" in result.stdout
    assert "streamlit run app.py" not in result.stdout
    assert "uv run" not in result.stdout
    assert "--require-hashes" in result.stdout


def test_start_rejects_premature_universal_mode_with_stable_reason(tmp_path: Path) -> None:
    data_dir = tmp_path / "demo-data"
    script = isolated_local_app(tmp_path)
    data_dir.mkdir()
    (data_dir / "clerksan.sqlite").touch()
    (data_dir / "doc_store").mkdir()

    result = run_local_app(
        "start",
        "--data-dir",
        str(data_dir),
        "--dry-run",
        cwd=tmp_path,
        script=script,
        environment=os.environ | {"CLERKSAN_INTAKE_MODE": "universal"},
    )

    assert result.returncode != 0
    assert result.stderr.strip() == "sandbox_unavailable"
    assert "uvicorn clerksan.api.main:app" not in result.stdout
    assert "clerksan.ingest.worker" not in result.stdout


def test_start_and_status_reject_a_symlinked_sqlite_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir()
    outside = tmp_path / "outside.sqlite"
    outside.touch()
    (data_dir / "clerksan.sqlite").symlink_to(outside)
    (data_dir / "doc_store").mkdir()
    script = isolated_local_app(tmp_path)

    dry_run = run_local_app(
        "start",
        "--data-dir",
        str(data_dir),
        "--dry-run",
        cwd=tmp_path,
        script=script,
    )
    status = run_local_app(
        "status",
        "--data-dir",
        str(data_dir),
        cwd=tmp_path,
        script=script,
    )

    assert dry_run.returncode != 0
    assert "Demo database is missing or unsafe" in dry_run.stderr
    assert status.returncode == 0, status.stderr
    assert f"Data: unsafe database path ({data_dir / 'clerksan.sqlite'})" in status.stdout


def test_start_keeps_intake_available_when_models_are_missing(tmp_path: Path) -> None:
    data_dir = tmp_path / "demo-data"
    script = isolated_local_app(tmp_path)
    data_dir.mkdir()
    (data_dir / "clerksan.sqlite").touch()
    (data_dir / "doc_store").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl_log = tmp_path / "curl.log"
    readiness = json.dumps(
        {
            "status": "ready",
            "demo_mode": False,
            "intake_ready": True,
            "review_ready": True,
            "processing_ready": False,
            "processing_reason_codes": ["model_unavailable"],
            "worker_registry_digest": "a" * 64,
            "worker_capabilities_digest": "b" * 64,
            "worker_capability_lease_age_seconds": 0.1,
        }
    )
    write_executable(
        bin_dir / "curl",
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$CLERKSAN_TEST_CURL_LOG"\n'
        'if [[ "$*" == *"/ready"* ]]; then\n'
        f"  printf '%s\\n' {shlex.quote(readiness)}\n"
        "fi\n"
        "exit 0\n",
    )
    write_executable(
        bin_dir / "ollama",
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "list" ]]; then\n'
        "  printf '%s\\n' 'NAME ID SIZE MODIFIED'\n"
        "fi\n",
    )
    write_executable(bin_dir / "lsof", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(bin_dir / "uv", "#!/usr/bin/env bash\nexit 91\n")
    write_long_running_launcher_python(tmp_path)
    write_successful_npm_stub(bin_dir)
    environment = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLERKSAN_TEST_CURL_LOG": str(curl_log),
        "CLERKSAN_TEST_EXPECTED_SETUP_LOCK": str(
            tmp_path / ".clerksan-runtime" / "python-env-setup.lock"
        ),
    }

    try:
        result = run_local_app(
            "start",
            "--data-dir",
            str(data_dir),
            cwd=tmp_path,
            script=script,
            environment=environment,
        )

        assert result.returncode == 0, result.stderr
        assert "Intake mode: legacy" in result.stdout
        assert "Ollama: reachable; missing" in result.stdout
        assert "Processing: delayed" in result.stdout
        assert "Started API" in result.stdout
        assert "Started Worker" in result.stdout
        assert "Core: ready for local intake and review (demo mode: false)" in result.stdout
        assert "Processing: unavailable (model_unavailable)" in result.stdout
        assert "Clerk-san core is available at http://127.0.0.1:8000" in result.stdout
        assert "/ready" in curl_log.read_text(encoding="utf-8")
        backups = list(
            (tmp_path / ".clerksan-runtime" / "sqlite-upgrade" / "pre-upgrade-backups").iterdir()
        )
        assert len(backups) == 1
        assert (backups[0] / "manifest.json").is_file()
        assert not (tmp_path / ".clerksan-runtime" / "python-env-setup.lock").exists()
    finally:
        run_local_app(
            "stop",
            "--data-dir",
            str(data_dir),
            cwd=tmp_path,
            script=script,
            environment=environment,
        )


def test_launcher_provisions_with_hashes_and_prunes_only_owned_stale_environments(
    tmp_path: Path,
) -> None:
    script = isolated_local_app(tmp_path)
    environments = tmp_path / ".clerksan-runtime" / "python-envs"
    stale_digest = "a" * 64
    stale = environments / stale_digest
    (stale / "bin").mkdir(parents=True)
    write_executable(stale / "bin" / "python", "#!/usr/bin/env bash\nexit 0\n")
    (stale / ".clerksan-lock-sha256").write_text(f"{stale_digest}\n", encoding="ascii")
    unowned_digest = "b" * 64
    unowned = environments / unowned_digest
    (unowned / "bin").mkdir(parents=True)
    write_executable(unowned / "bin" / "python", "#!/usr/bin/env bash\nexit 0\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_log = tmp_path / "uv.log"
    write_provisioning_uv_stub(bin_dir)
    write_executable(bin_dir / "curl", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(
        bin_dir / "ollama",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' 'NAME ID SIZE MODIFIED'\n"
        "printf '%s\\n' 'gemma3:4b x x x'\n"
        "printf '%s\\n' 'qwen2.5:7b x x x'\n"
        "printf '%s\\n' 'nomic-embed-text:v1.5 x x x'\n",
    )
    environment = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLERKSAN_TEST_UV_LOG": str(uv_log),
        "CLERKSAN_INTAKE_MODE": "universal",
        "CLERKSAN_DEMO_MODE": "true",
    }

    result = run_local_app(
        "init-demo",
        "--data-dir",
        str(tmp_path / "fresh-demo"),
        cwd=tmp_path,
        script=script,
        environment=environment,
    )

    assert result.returncode == 0, result.stderr
    digest = hashlib.sha256((tmp_path / "requirements.lock").read_bytes()).hexdigest()
    active = environments / digest
    assert (active / "bin" / "python").is_file()
    assert (active / ".clerksan-lock-sha256").read_text(encoding="ascii") == f"{digest}\n"
    assert not stale.exists()
    assert unowned.exists()
    uv_commands = uv_log.read_text(encoding="utf-8")
    assert "pip install" in uv_commands
    assert "--require-hashes" in uv_commands
    assert "--no-deps" in uv_commands
    assert "--requirement" in uv_commands


def test_launcher_refuses_a_symlinked_dependency_environment_root(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir()
    (data_dir / "clerksan.sqlite").touch()
    (data_dir / "doc_store").mkdir()
    runtime = tmp_path / ".clerksan-runtime"
    runtime.mkdir()
    outside = tmp_path / "outside-environments"
    outside.mkdir()
    (runtime / "python-envs").symlink_to(outside, target_is_directory=True)
    matching_environment = write_long_running_launcher_python(tmp_path)
    assert matching_environment.resolve().is_relative_to(outside)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "uv", "#!/usr/bin/env bash\nexit 99\n")

    result = run_local_app(
        "start",
        "--data-dir",
        str(data_dir),
        cwd=tmp_path,
        script=script,
        environment=os.environ | {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert result.returncode != 0
    assert "dependency path must be a regular directory" in result.stderr
    assert matching_environment.resolve().is_dir()


def test_launcher_installs_from_an_immutable_verified_lock_snapshot(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    lock = tmp_path / "requirements.lock"
    original_lock = lock.read_bytes()
    original_digest = hashlib.sha256(original_lock).hexdigest()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_log = tmp_path / "uv.log"
    write_provisioning_uv_stub(bin_dir)
    write_executable(bin_dir / "curl", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(
        bin_dir / "ollama",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' 'NAME ID SIZE MODIFIED'\n"
        "printf '%s\\n' 'gemma3:4b x x x'\n"
        "printf '%s\\n' 'qwen2.5:7b x x x'\n"
        "printf '%s\\n' 'nomic-embed-text:v1.5 x x x'\n",
    )
    environment = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLERKSAN_TEST_UV_LOG": str(uv_log),
        "CLERKSAN_TEST_MUTATE_LOCK": str(lock),
    }

    result = run_local_app(
        "init-demo",
        "--data-dir",
        str(tmp_path / "fresh-demo"),
        cwd=tmp_path,
        script=script,
        environment=environment,
    )

    assert result.returncode == 0, result.stderr
    assert hashlib.sha256(lock.read_bytes()).hexdigest() != original_digest
    published = tmp_path / ".clerksan-runtime" / "python-envs" / original_digest
    snapshot = published / ".clerksan-requirements.lock"
    assert snapshot.read_bytes() == original_lock
    assert (published / ".clerksan-lock-sha256").read_text(encoding="ascii") == (
        f"{original_digest}\n"
    )
    pip_command = next(
        line for line in uv_log.read_text(encoding="utf-8").splitlines() if "pip install" in line
    )
    assert ".clerksan-requirements.lock" in pip_command
    assert str(lock) not in pip_command


def test_stop_terminates_only_a_launcher_owned_process(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir()
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "uvicorn clerksan.api.main:app",
        ]
    )
    record = write_pid_record(tmp_path / ".clerksan-runtime", pid=process.pid, data_dir=data_dir)
    try:
        result = run_local_app("stop", "--data-dir", str(data_dir), cwd=tmp_path, script=script)

        assert result.returncode == 0, result.stderr
        process.wait(timeout=5)
        assert not record.exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_stop_refuses_to_signal_a_pid_record_with_an_unrelated_command(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    record = write_pid_record(tmp_path / ".clerksan-runtime", pid=process.pid, data_dir=data_dir)
    try:
        result = run_local_app("stop", "--data-dir", str(data_dir), cwd=tmp_path, script=script)

        assert result.returncode != 0
        assert "unsafe launcher record" in result.stderr
        assert process.poll() is None
        assert record.exists()
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_start_refuses_an_unknown_port_occupant_before_launching_any_role(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir()
    (data_dir / "clerksan.sqlite").touch()
    (data_dir / "doc_store").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "curl", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(
        bin_dir / "ollama",
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "list" ]]; then\n'
        "  printf '%s\\n' 'NAME ID SIZE MODIFIED'\n"
        "  printf '%s\\n' 'gemma3:4b x x x'\n"
        "  printf '%s\\n' 'qwen2.5:7b x x x'\n"
        "  printf '%s\\n' 'nomic-embed-text:v1.5 x x x'\n"
        "fi\n",
    )
    write_executable(bin_dir / "lsof", "#!/usr/bin/env bash\nprintf '42424\\n'\n")
    write_long_running_launcher_python(tmp_path)
    write_successful_npm_stub(bin_dir)
    environment = os.environ | {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    result = run_local_app(
        "start",
        "--data-dir",
        str(data_dir),
        cwd=tmp_path,
        script=script,
        environment=environment,
    )

    assert result.returncode != 0
    assert "already in use by PID 42424" in result.stderr
    assert not (tmp_path / ".clerksan-runtime" / "api.pid").exists()


def test_start_checks_readiness_before_accepting_an_existing_api_process(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir()
    (data_dir / "clerksan.sqlite").touch()
    (data_dir / "doc_store").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(
        bin_dir / "curl",
        '#!/usr/bin/env bash\nif [[ "$*" == *"/api/tags"* ]]; then\n  exit 0\nfi\nexit 22\n',
    )
    write_executable(
        bin_dir / "ollama",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' 'NAME ID SIZE MODIFIED'\n"
        "printf '%s\\n' 'gemma3:4b x x x'\n"
        "printf '%s\\n' 'qwen2.5:7b x x x'\n"
        "printf '%s\\n' 'nomic-embed-text:v1.5 x x x'\n",
    )
    write_executable(bin_dir / "lsof", "#!/usr/bin/env bash\nexit 0\n")
    write_long_running_launcher_python(tmp_path)
    write_successful_npm_stub(bin_dir)
    environment = os.environ | {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "uvicorn clerksan.api.main:app",
        ]
    )
    try:
        write_pid_record(tmp_path / ".clerksan-runtime", pid=process.pid, data_dir=data_dir)

        result = run_local_app(
            "start",
            "--data-dir",
            str(data_dir),
            cwd=tmp_path,
            script=script,
            environment=environment,
        )

        assert result.returncode != 0
        assert "normal-mode core readiness is unavailable" in result.stderr
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_status_rejects_a_symlinked_launcher_runtime_directory(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    runtime_target = tmp_path / "outside-runtime"
    runtime_target.mkdir()
    (tmp_path / ".clerksan-runtime").symlink_to(runtime_target, target_is_directory=True)

    result = run_local_app("status", cwd=tmp_path, script=script)

    assert result.returncode != 0
    assert "Launcher runtime: unsafe path" in result.stdout


def test_start_refuses_symlinked_log_without_truncating_target(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir()
    (data_dir / "clerksan.sqlite").touch()
    (data_dir / "doc_store").mkdir()
    _, environment = write_launcher_runtime_stubs(tmp_path)
    protected = tmp_path / "protected.txt"
    protected.write_text("preserve\n", encoding="utf-8")
    (tmp_path / ".clerksan-runtime" / "api.log").symlink_to(protected)

    result = run_local_app(
        "start",
        "--data-dir",
        str(data_dir),
        cwd=tmp_path,
        script=script,
        environment=environment,
    )

    assert result.returncode != 0
    assert "log path is unsafe" in result.stderr
    assert protected.read_text(encoding="utf-8") == "preserve\n"
    assert not (tmp_path / ".clerksan-runtime" / "api.pid").exists()


def test_dependency_setup_refuses_an_active_owned_lock(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir()
    (data_dir / "clerksan.sqlite").touch()
    (data_dir / "doc_store").mkdir()
    write_long_running_launcher_python(tmp_path)
    lock = tmp_path / ".clerksan-runtime" / "python-env-setup.lock"
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock.write_text(
            f"clerksan-local-app-v2\n{owner.pid}\n{process_start_token(owner.pid)}\n",
            encoding="utf-8",
        )

        result = run_local_app(
            "start",
            "--data-dir",
            str(data_dir),
            cwd=tmp_path,
            script=script,
        )

        assert result.returncode != 0
        assert "dependency setup is in progress" in result.stderr
        assert owner.poll() is None
        assert lock.is_file()
    finally:
        owner.terminate()
        owner.wait(timeout=5)


def test_dependency_setup_recovers_only_a_well_formed_stale_lock(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir()
    (data_dir / "clerksan.sqlite").touch()
    (data_dir / "doc_store").mkdir()
    _, environment = write_launcher_runtime_stubs(tmp_path)
    runtime = tmp_path / ".clerksan-runtime"
    lock = runtime / "python-env-setup.lock"
    lock.write_text(
        "clerksan-local-app-v2\n99999999\nMon Jan  1 00:00:00 2001\n",
        encoding="utf-8",
    )
    protected = tmp_path / "protected.txt"
    protected.write_text("preserve\n", encoding="utf-8")
    (runtime / "api.log").symlink_to(protected)

    result = run_local_app(
        "start",
        "--data-dir",
        str(data_dir),
        cwd=tmp_path,
        script=script,
        environment=environment,
    )

    assert result.returncode != 0
    assert "log path is unsafe" in result.stderr
    assert not lock.exists()
    assert protected.read_text(encoding="utf-8") == "preserve\n"


@pytest.mark.parametrize("abandoned", ("", "clerksan-local-app-v2\n"))
def test_dependency_setup_quarantines_an_abandoned_partial_lock(
    tmp_path: Path,
    abandoned: str,
) -> None:
    script = isolated_local_app(tmp_path)
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir()
    (data_dir / "clerksan.sqlite").touch()
    (data_dir / "doc_store").mkdir()
    _, environment = write_launcher_runtime_stubs(tmp_path)
    runtime = tmp_path / ".clerksan-runtime"
    lock = runtime / "python-env-setup.lock"
    lock.write_text(abandoned, encoding="utf-8")
    protected = tmp_path / "protected.txt"
    protected.write_text("preserve\n", encoding="utf-8")
    (runtime / "api.log").symlink_to(protected)

    result = run_local_app(
        "start",
        "--data-dir",
        str(data_dir),
        cwd=tmp_path,
        script=script,
        environment=environment,
    )

    assert result.returncode != 0
    assert "Quarantined an abandoned dependency setup lock" in result.stderr
    assert "log path is unsafe" in result.stderr
    assert not lock.exists()
    assert protected.read_text(encoding="utf-8") == "preserve\n"
    quarantines = list(runtime.glob("python-env-setup.lock.abandoned-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_text(encoding="utf-8") == abandoned


def test_dependency_setup_quarantines_a_legacy_empty_lock_directory(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir()
    (data_dir / "clerksan.sqlite").touch()
    (data_dir / "doc_store").mkdir()
    _, environment = write_launcher_runtime_stubs(tmp_path)
    runtime = tmp_path / ".clerksan-runtime"
    lock = runtime / "python-env-setup.lock"
    lock.mkdir()
    protected = tmp_path / "protected.txt"
    protected.write_text("preserve\n", encoding="utf-8")
    (runtime / "api.log").symlink_to(protected)

    result = run_local_app(
        "start",
        "--data-dir",
        str(data_dir),
        cwd=tmp_path,
        script=script,
        environment=environment,
    )

    assert result.returncode != 0
    assert "Quarantined an abandoned dependency setup lock" in result.stderr
    assert "log path is unsafe" in result.stderr
    assert not lock.exists()
    quarantines = list(runtime.glob("python-env-setup.lock.abandoned-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "lock").is_dir()


def test_dependency_lock_publication_failure_leaves_no_fixed_or_partial_lock(
    tmp_path: Path,
) -> None:
    script = isolated_local_app(tmp_path)
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir()
    (data_dir / "clerksan.sqlite").touch()
    (data_dir / "doc_store").mkdir()
    bin_dir, environment = write_launcher_runtime_stubs(tmp_path)
    write_executable(bin_dir / "ln", "#!/usr/bin/env bash\nexit 1\n")
    runtime = tmp_path / ".clerksan-runtime"

    result = run_local_app(
        "start",
        "--data-dir",
        str(data_dir),
        cwd=tmp_path,
        script=script,
        environment=environment,
    )

    assert result.returncode != 0
    assert "lock acquisition" in result.stderr
    assert not (runtime / "python-env-setup.lock").exists()
    assert list(runtime.glob(".python-env-setup.owner.*")) == []


def test_status_uses_configured_model_names_instead_of_hardcoded_defaults(
    tmp_path: Path,
) -> None:
    script = isolated_local_app(tmp_path)
    _, environment = write_launcher_runtime_stubs(
        tmp_path,
        models=(
            "custom-ocr:1",
            "custom-extract:2",
            "custom-router:3",
            "nomic-embed-text:v1.5",
        ),
    )
    environment |= {
        "CLERKSAN_OCR_MODEL": "custom-ocr:1",
        "CLERKSAN_EXTRACT_MODEL": "custom-extract:2",
        "CLERKSAN_ROUTER_MODEL": "custom-router:3",
    }

    result = run_local_app("status", cwd=tmp_path, script=script, environment=environment)

    assert result.returncode == 0, result.stderr
    assert "Ollama: reachable with required models" in result.stdout
    assert "gemma3:4b" not in result.stdout
    assert "qwen2.5:7b" not in result.stdout


def test_status_treats_an_implicit_latest_tag_as_the_configured_model(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    _, environment = write_launcher_runtime_stubs(
        tmp_path,
        models=(
            "custom-ocr:latest",
            "custom-extract:latest",
            "nomic-embed-text:v1.5",
        ),
    )
    environment |= {
        "CLERKSAN_OCR_MODEL": "custom-ocr",
        "CLERKSAN_EXTRACT_MODEL": "custom-extract",
        "CLERKSAN_ROUTER_MODEL": "custom-extract",
    }

    result = run_local_app("status", cwd=tmp_path, script=script, environment=environment)

    assert result.returncode == 0, result.stderr
    assert "Ollama: reachable with required models" in result.stdout
    assert "missing custom" not in result.stdout


def test_status_bounds_an_unresponsive_ollama_cli(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    bin_dir, environment = write_launcher_runtime_stubs(tmp_path)
    write_executable(
        bin_dir / "ollama",
        "#!/usr/bin/env bash\n"
        f"exec {shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'\n",
    )

    started = time.monotonic()
    result = run_local_app("status", cwd=tmp_path, script=script, environment=environment)
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert elapsed < 8
    assert "Ollama: reachable, but models could not be listed" in result.stdout


def test_worker_pid_publication_failure_cleans_up_the_started_api(tmp_path: Path) -> None:
    script = isolated_local_app(tmp_path)
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir()
    (data_dir / "clerksan.sqlite").touch()
    (data_dir / "doc_store").mkdir()
    bin_dir, environment = write_launcher_runtime_stubs(tmp_path)
    system_mktemp = shutil.which("mktemp")
    assert system_mktemp is not None
    write_executable(
        bin_dir / "mktemp",
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == *"/.worker.pid."* ]]; then exit 1; fi\n'
        f'exec {shlex.quote(system_mktemp)} "$@"\n',
    )

    result = run_local_app(
        "start",
        "--data-dir",
        str(data_dir),
        cwd=tmp_path,
        script=script,
        environment=environment,
    )

    assert result.returncode != 0
    assert "Could not safely record the Worker process ID" in result.stderr
    assert "stopping only roles started during this attempt" in result.stderr
    assert not (tmp_path / ".clerksan-runtime" / "api.pid").exists()
    assert not (tmp_path / ".clerksan-runtime" / "worker.pid").exists()
    started_api = re.search(r"Started API \(PID ([0-9]+)\)", result.stdout)
    assert started_api is not None
    process_check = subprocess.run(
        ["ps", "-p", started_api.group(1)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process_check.returncode != 0
