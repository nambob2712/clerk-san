"""Static and optional runtime checks for the Compose/container foundation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from clerksan.config import Settings

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.yml"
DOCKERFILE_PATH = ROOT / "Dockerfile"
DOCKERIGNORE_PATH = ROOT / ".dockerignore"
REQUIREMENTS_PATH = ROOT / "requirements.txt"
RESTORE_SCRIPT_PATH = ROOT / "scripts" / "restore.sh"
BACKUP_SCRIPT_PATH = ROOT / "scripts" / "backup.sh"


def service_block(compose: str, service: str) -> str:
    """Return one top-level service block from this deliberately simple Compose file."""
    pattern = (
        rf"(?ms)^  {re.escape(service)}:\n(?P<body>.*?)"
        r"(?=^  [a-z][a-z0-9_-]*:\n|^volumes:|^networks:|\Z)"
    )
    match = re.search(pattern, compose)
    if match is None:
        raise AssertionError(f"missing {service!r} service")
    return match.group("body")


class InfrastructureContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compose = COMPOSE_PATH.read_text(encoding="utf-8")
        cls.dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        cls.dockerignore = DOCKERIGNORE_PATH.read_text(encoding="utf-8")
        cls.requirements = REQUIREMENTS_PATH.read_text(encoding="utf-8")
        cls.restore_script = RESTORE_SCRIPT_PATH.read_text(encoding="utf-8")
        cls.backup_script = BACKUP_SCRIPT_PATH.read_text(encoding="utf-8")

    def test_data_services_have_no_host_ports(self) -> None:
        for service in ("db", "ollama"):
            with self.subTest(service=service):
                self.assertNotRegex(service_block(self.compose, service), r"(?m)^    ports:")

    def test_api_host_mapping_is_loopback_only(self) -> None:
        api = service_block(self.compose, "api")
        self.assertRegex(api, r'(?m)^    ports:\n      - "127\.0\.0\.1:8000:8000"$')
        self.assertNotRegex(api, r'(?m)^      - "(?:0\.0\.0\.0|8000):8000"$')

    def test_worker_has_no_ports_and_apps_are_inert_by_default(self) -> None:
        worker = service_block(self.compose, "worker")
        self.assertNotRegex(worker, r"(?m)^    ports:")
        for service in ("api", "worker"):
            self.assertRegex(service_block(self.compose, service), r'(?m)^    profiles: \["app"\]$')
        self.assertRegex(
            worker,
            r"(?ms)^    depends_on:\n.*?^      api:\n        condition: service_started$",
        )
        self.assertIn("http://127.0.0.1:8000/ready", service_block(self.compose, "api"))

    def test_persistent_volumes_and_healthchecks_are_declared(self) -> None:
        db = service_block(self.compose, "db")
        ollama = service_block(self.compose, "ollama")
        self.assertIn("pgdata:/var/lib/postgresql/data", db)
        self.assertIn("ollama_models:/root/.ollama", ollama)
        self.assertRegex(db, r"(?m)^    healthcheck:")
        self.assertRegex(ollama, r"(?m)^    healthcheck:")
        for volume in ("pgdata", "ollama_models", "doc_store", "parser_socket"):
            self.assertRegex(self.compose, rf"(?m)^  {volume}:$")

    def test_parser_sidecar_is_dark_fd_only_and_hard_sandboxed(self) -> None:
        parser = service_block(self.compose, "parser")
        self.assertIn("target: parser-runtime", parser)
        self.assertRegex(parser, r'(?m)^    profiles: \["universal"\]$')
        self.assertNotIn('profiles: ["app"]', parser)
        self.assertRegex(parser, r'(?m)^    network_mode: "none"$')
        self.assertRegex(parser, r"(?m)^    read_only: true$")
        self.assertRegex(parser, r"(?ms)^    cap_drop:\n      - ALL$")
        self.assertRegex(parser, r"(?ms)^    security_opt:\n      - no-new-privileges:true$")
        for setting in ("pids_limit:", "mem_limit:", "cpus:", "tmpfs:"):
            with self.subTest(setting=setting):
                self.assertIn(setting, parser)
        self.assertIn("/tmp:rw,noexec,nosuid,nodev,size=64m", parser)
        self.assertIn("parser_socket:/run/clerksan-parser", parser)
        self.assertNotRegex(parser, r"(?m)^    networks:")
        for forbidden in (
            "CLERKSAN_DATABASE",
            "CLERKSAN_OLLAMA",
            "CLERKSAN_STORAGE",
            "POSTGRES_PASSWORD",
            "doc_store:",
            "ollama_models:",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parser)

        for service in ("api", "worker"):
            block = service_block(self.compose, service)
            self.assertIn("parser_socket:/run/clerksan-parser:ro", block)
            self.assertIn(
                "CLERKSAN_INTAKE_MODE: ${CLERKSAN_INTAKE_MODE:-legacy}",
                block,
            )
        api = service_block(self.compose, "api")
        self.assertRegex(
            api,
            r"(?ms)^    depends_on:\n.*?^      parser:\n"
            r"        condition: service_healthy\n        required: false$",
        )

    def test_compose_uses_required_password_interpolation(self) -> None:
        required = "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
        self.assertGreaterEqual(self.compose.count(required), 3)
        self.assertNotIn("POSTGRES_PASSWORD: clerksan", self.compose)

    def test_compose_encodes_reserved_database_password_characters_outside_the_url(self) -> None:
        expected_url = (
            "postgresql+asyncpg://${POSTGRES_USER:-clerksan}@db:5432/${POSTGRES_DB:-clerksan}"
        )
        required_password = (
            "CLERKSAN_DATABASE_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
        )
        for service in ("api", "worker"):
            block = service_block(self.compose, service)
            self.assertIn(f"CLERKSAN_DATABASE_URL: {expected_url}", block)
            self.assertIn(required_password, block)
            self.assertNotRegex(block, r"CLERKSAN_DATABASE_URL:.*POSTGRES_PASSWORD")

        settings = Settings(
            database_url="postgresql+asyncpg://clerksan@db:5432/clerksan",
            database_password="p@ss:/?#%word",
        )
        self.assertEqual(
            settings.database_url,
            "postgresql+asyncpg://clerksan:p%40ss%3A%2F%3F%23%25word@db:5432/clerksan",
        )

    def test_dockerfile_is_non_root_and_supports_api_and_worker(self) -> None:
        self.assertIn("FROM python:3.11-slim-bookworm", self.dockerfile)
        self.assertIn("libmagic1", self.dockerfile)
        self.assertIn("USER clerksan", self.dockerfile)
        self.assertIn('CMD ["uvicorn", "clerksan.api.main:app"', self.dockerfile)
        self.assertNotIn("COPY . .", self.dockerfile)
        self.assertIn('command: ["python", "-m", "clerksan.ingest.worker"]', self.compose)

    def test_dockerfile_has_a_dedicated_credential_free_parser_target(self) -> None:
        self.assertIn("FROM python-runtime AS parser-runtime", self.dockerfile)
        self.assertIn("FROM python-runtime AS app-runtime", self.dockerfile)
        self.assertIn(
            'ENTRYPOINT ["python", "-m", "clerksan.ingest.parser_service"]',
            self.dockerfile,
        )
        parser_start = self.dockerfile.index("FROM python-runtime AS parser-runtime")
        app_start = self.dockerfile.index("FROM python-runtime AS app-runtime")
        migrations_copy = self.dockerfile.index("COPY --chown=clerksan:clerksan migrations/")
        self.assertLess(parser_start, app_start)
        self.assertGreater(migrations_copy, app_start)

    def test_build_context_excludes_local_document_outputs(self) -> None:
        for pattern in (".clerksan-*/", "backups/", "data/", "*.sqlite"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, self.dockerignore)

    def test_restore_keeps_prior_storage_until_the_database_restore_succeeds(self) -> None:
        self.assertIn(
            '"${compose[@]}" stop --timeout "$stop_timeout" api worker', self.restore_script
        )
        self.assertIn("-v ON_ERROR_STOP=1", self.restore_script)
        self.assertIn("--single-transaction", self.restore_script)
        self.assertIn("trap recover_after_failure EXIT", self.restore_script)
        self.assertIn("rollback_store", self.restore_script)
        self.assertIn(
            "Database restore failed; rolling the document store back.",
            self.restore_script,
        )
        self.assertLess(
            self.restore_script.index("stage_store\nrestore_active=true\nactivate_store"),
            self.restore_script.rindex('if ! "${compose[@]}" exec -T db psql'),
        )
        self.assertIn("replacement-active", self.restore_script)
        self.assertIn("resume_previously_running_services", self.restore_script)
        self.assertIn('"${compose[@]}" up -d "${running_services[@]}"', self.restore_script)

    def test_restore_verifies_inventory_before_discarding_prior_storage(self) -> None:
        self.assertIn("CLERKSAN_RESTORE_MODE", self.restore_script)
        self.assertIn("verify_restored_inventory", self.restore_script)
        self.assertIn("database-inventory.json", self.restore_script)
        self.assertLess(
            self.restore_script.index("database_restored=true\nif ! verify_restored_inventory"),
            self.restore_script.index("restore_active=false\nif ! discard_previous_store"),
        )

    def test_backup_streams_the_document_store_through_a_transient_api_service(self) -> None:
        self.assertIn('"${compose[@]}" run --rm --no-deps -T api python -c', self.backup_script)
        self.assertIn('tarfile.open(fileobj=sys.stdout.buffer, mode="w|")', self.backup_script)
        self.assertIn("unsafe document-store archive member", self.backup_script)
        self.assertNotIn("cp api:/data/doc_store", self.backup_script)

    def test_backup_fences_leases_and_excludes_runtime_storage(self) -> None:
        self.assertIn("CLERKSAN_BACKUP_MODE", self.backup_script)
        self.assertIn("maintenance-preflight", self.backup_script)
        self.assertIn("database-inventory", self.backup_script)
        self.assertIn('relative.parts[0] == ".quarantine"', self.backup_script)
        self.assertIn('relative == Path(".storage.lock")', self.backup_script)
        self.assertLess(
            self.backup_script.index("maintenance-preflight"),
            self.backup_script.index("exec -T db pg_dump"),
        )

    def test_requirements_cover_v2_runtime_and_test_contracts(self) -> None:
        for package in (
            "fastapi",
            "uvicorn[standard]",
            "pydantic",
            "pydantic-settings",
            "SQLAlchemy[asyncio]",
            "asyncpg",
            "pgvector",
            "httpx",
            "python-magic",
            "pypdf",
            "pypdfium2",
            "Pillow",
            "mammoth",
            "python-docx",
            "openpyxl",
            "pandas",
            "ImageHash",
            "RapidFuzz",
            "pytest",
            "pytest-asyncio",
        ):
            with self.subTest(package=package):
                self.assertRegex(self.requirements, rf"(?m)^{re.escape(package)}>=[^\n]+$")

    @unittest.skipUnless(shutil.which("docker"), "Docker is not installed")
    def test_docker_compose_config_validates_with_a_supplied_password(self) -> None:
        environment = os.environ | {"POSTGRES_PASSWORD": "test-only-password"}
        result = subprocess.run(
            ["docker", "compose", "config"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
