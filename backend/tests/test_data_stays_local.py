"""Personal data (resumes, drafts, prompts and responses) lives in backend/data and must never travel with the repository,
which may be public. These tests fail if someone loosens .gitignore or starts tracking data files."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs git and a checkout")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


@pytest.mark.parametrize("path", [
    "backend/data/aiops.db", "backend/data/aiops.db-wal", "backend/data/aiops.db-shm",  # the live database and its WAL files
    "backend/data_export/resources.jsonl", "backend/data_export/events/some-run.jsonl",  # text exports
    "backend/runs/old-run.jsonl",  # the pre-SQLite run folder
    "backend/.env",  # gateway credentials
])
def test_personal_data_paths_are_git_ignored(path):
    assert git("check-ignore", "-q", path).returncode == 0, f"{path} is not ignored: it could be committed to a public repository"


def test_nothing_under_the_data_folders_is_tracked():
    tracked = git("ls-files", "backend/data", "backend/data_export", "backend/runs").stdout.split()
    assert tracked == [], f"personal data is tracked by git: {tracked}"


def test_real_data_is_not_hiding_in_a_tracked_test_fixture():
    # fixtures are committed on purpose, so they must stay fictional and small
    fixtures = [p for p in (ROOT / "backend" / "tests" / "fixtures").iterdir()]
    assert fixtures and all(p.stat().st_size < 100_000 for p in fixtures)
