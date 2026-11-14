import gzip
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def backup_workspace(tmp_path):
    return tmp_path


def run_backup(directory, dump_exit=0, backup_dir=None, extra_env=None, mode="--container"):
    binary = directory / "bin"
    binary.mkdir(exist_ok=True)
    executable = binary / "pg_dump"
    executable.write_text(f"#!/bin/sh\nprintf 'sample database SQL\\n'\nexit {dump_exit}\n")
    executable.chmod(0o700)
    env = dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}",
               BACKUP_DIR=str(backup_dir or directory), PGPASSWORD="test")
    env.update(extra_env or {})
    return subprocess.run(["sh", str(ROOT / "backup.sh"), mode],
                          env=env, capture_output=True, text=True, timeout=10)


def test_successful_backup_is_valid_compressed_sql_and_cleans_partials(backup_workspace):
    result = run_backup(backup_workspace)
    assert result.returncode == 0, result.stderr
    files = list(backup_workspace.glob("streamclock-*.sql.gz"))
    assert len(files) == 1
    assert gzip.decompress(files[0].read_bytes()) == b"sample database SQL\n"
    assert not list(backup_workspace.glob(".*.partial.*"))


def test_failed_dump_preserves_existing_backup_and_cleans_partials(backup_workspace):
    day = subprocess.check_output(["date", "-u", "+%F"], text=True).strip()
    existing = backup_workspace / f"streamclock-{day}.sql.gz"
    existing.write_bytes(b"existing successful backup")
    result = run_backup(backup_workspace, dump_exit=1)
    assert result.returncode != 0
    assert existing.read_bytes() == b"existing successful backup"
    assert not list(backup_workspace.glob(".*.partial.*"))


def test_missing_backup_path_is_not_created(backup_workspace):
    missing = backup_workspace / "unmounted-ssd"
    result = run_backup(backup_workspace, backup_dir=missing)
    assert result.returncode != 0
    assert "directory does not exist" in result.stderr
    assert not missing.exists()


def test_missing_required_ssd_mount_refuses_backup(backup_workspace):
    result = run_backup(backup_workspace, extra_env={"BACKUP_MOUNT": str(backup_workspace)})
    assert result.returncode != 0
    assert "SSD mount is missing" in result.stderr
    assert not list(backup_workspace.glob("streamclock-*.sql.gz"))


def test_success_applies_retention_only_to_matching_archives(backup_workspace):
    old = backup_workspace / "streamclock-2020-01-01.sql.gz"
    unrelated = backup_workspace / "important.txt"
    for path in (old, unrelated):
        path.write_text("keep until successful backup")
        os.utime(path, (1, 1))
    assert run_backup(backup_workspace, dump_exit=1).returncode != 0
    assert old.exists()
    assert run_backup(backup_workspace).returncode == 0
    assert not old.exists()
    assert unrelated.exists()


def test_failed_compression_never_promotes_archive(backup_workspace):
    binary = backup_workspace / "bin"
    binary.mkdir()
    compressor = binary / "gzip"
    compressor.write_text("#!/bin/sh\nprintf partial\nexit 1\n")
    compressor.chmod(0o700)
    result = run_backup(backup_workspace)
    assert result.returncode != 0
    assert not list(backup_workspace.glob("streamclock-*.sql.gz"))
    assert not list(backup_workspace.glob(".*.partial.*"))


def test_scheduled_failure_is_reported_without_publishing_partial_dump(backup_workspace):
    binary = backup_workspace / "bin"
    binary.mkdir()
    sleeper = binary / "sleep"
    sleeper.write_text(
        '#!/bin/sh\n'
        'if [ -f "$BACKUP_DIR/slept" ]; then exit 1; fi\n'
        'touch "$BACKUP_DIR/slept"\n'
    )
    sleeper.chmod(0o700)
    result = run_backup(backup_workspace, dump_exit=1, mode="--schedule")
    assert "Nightly backup FAILED" in result.stderr
    assert not list(backup_workspace.glob("streamclock-*.sql.gz"))
    assert not list(backup_workspace.glob(".*.partial.*"))


def test_compose_automatically_schedules_backups_without_creating_host_path():
    compose = (ROOT / "docker-compose.yml").read_text()
    script = (ROOT / "backup.sh").read_text()
    assert 'entrypoint: ["/bin/sh", "/usr/local/bin/streamclock-backup", "--schedule"]' in compose
    assert "create_host_path: false" in compose
    assert "source: ${BACKUP_DIR:-./backups}" in compose
    assert "10800" in script and "date -u +%s" in script
