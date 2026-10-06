"""Migrations can be undone and redone (Phase 17: "can a bad migration be reverted cleanly").

Run as subprocesses against a scratch SQLite database, so the test database the rest of the suite
uses is never touched. The same command runs against a scratch Postgres in CI.
"""

import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def manage(*args, database_url: str, timeout=600):
    env = {**os.environ, "DATABASE_URL": database_url, "DJANGO_SETTINGS_MODULE": "config.settings"}
    return subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_every_migration_can_be_rolled_back_and_applied_again(tmp_path):
    result = manage(
        "check_migrations_reversible", database_url=f"sqlite:///{tmp_path / 'scratch.db'}"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Every migration is reversible." in result.stdout
    assert "No drift between the migrations and the models" in result.stdout
    for app in ("accounts", "monitoring", "providers", "statuspages", "apikeys"):
        assert f"Back to zero: {app}" in result.stdout


def test_it_refuses_to_run_against_a_database_that_is_not_a_scratch_one():
    """It drops every table, so a real-looking database must be refused before anything runs."""
    for url in (
        "postgres://user:pass@localhost:5432/rootpulse",
        "postgres://user:pass@db.example.com/production",
    ):
        result = manage("check_migrations_reversible", database_url=url, timeout=120)
        assert result.returncode != 0
        assert "Refusing to run" in result.stderr


def test_no_migration_is_one_way():
    """Every migration is schema-only (so Django can reverse it itself) or says how to go back."""
    risky = []
    for path in sorted(BASE_DIR.glob("*/migrations/0*.py")):
        text = path.read_text(encoding="utf-8")
        for operation in ("RunPython", "RunSQL"):
            if operation in text and "reverse" not in text:
                risky.append(f"{path.relative_to(BASE_DIR)}: {operation} without a reverse")
    assert risky == []
