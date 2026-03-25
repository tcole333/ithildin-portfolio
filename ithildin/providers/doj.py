"""DOJ provider adapter."""

from __future__ import annotations

from ithildin.core.process import run_python_script
from ithildin.core.paths import repo_root


def run(args: list[str]) -> int:
    return run_python_script(repo_root() / "tools" / "query_doj.py", args)
