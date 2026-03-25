"""Process helpers for the canonical Ithildin CLI surface."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def merged_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if overrides:
        env.update({key: value for key, value in overrides.items() if value is not None})
    return env


def run_python_script(
    script_path: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> int:
    command = [sys.executable, str(script_path), *args]
    completed = subprocess.run(command, env=merged_env(env), cwd=cwd)
    return completed.returncode


def run_command(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> int:
    completed = subprocess.run(command, env=merged_env(env), cwd=cwd)
    return completed.returncode
