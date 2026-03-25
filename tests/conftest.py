from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def fixtures_root(repo_root: Path) -> Path:
    return repo_root / "tests" / "fixtures"


@pytest.fixture(scope="session")
def fixtures_data_dir(fixtures_root: Path) -> Path:
    return fixtures_root / "data"


@pytest.fixture
def copy_fixture_db(tmp_path: Path, fixtures_data_dir: Path) -> Callable[[str], Path]:
    def _copy(name: str) -> Path:
        source = fixtures_data_dir / name
        if not source.exists():
            raise FileNotFoundError(f"Fixture DB not found: {source}")
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return target

    return _copy


@pytest.fixture
def copy_fixture_tree(tmp_path: Path, fixtures_data_dir: Path) -> Callable[[str], Path]:
    def _copy(relative_dir: str) -> Path:
        source = fixtures_data_dir / relative_dir
        if not source.exists():
            raise FileNotFoundError(f"Fixture directory not found: {source}")
        target = tmp_path / relative_dir
        shutil.copytree(source, target)
        return target

    return _copy


@pytest.fixture
def run_python_script(repo_root: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    def _run(script_relpath: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        script_path = repo_root / script_relpath
        if not script_path.exists():
            raise FileNotFoundError(f"Script not found: {script_path}")
        return subprocess.run(
            [sys.executable, str(script_path), *args],
            cwd=str(cwd or repo_root),
            capture_output=True,
            text=True,
            check=False,
        )

    return _run
