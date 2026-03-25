"""Build adapters for public Ithildin workflows."""

from __future__ import annotations

from pathlib import Path

from ithildin.core.paths import (
    portfolio_demo_content_root,
    portfolio_demo_doj_db,
    portfolio_demo_investigation_db,
    portfolio_demo_registry_db,
    repo_root,
)
from ithildin.core.process import run_command


ROOT = repo_root()
WEB_DIR = ROOT / "web"


def demo_env() -> dict[str, str]:
    return {
        "ITHILDIN_CONTENT_ROOT": str(portfolio_demo_content_root()),
        "ITHILDIN_INVESTIGATION_DB": str(portfolio_demo_investigation_db()),
        "ITHILDIN_DOJ_DB": str(portfolio_demo_doj_db()),
        "ITHILDIN_REGISTRY_DB": str(portfolio_demo_registry_db()),
        "PUBLIC_ENABLE_EVIDENCE_MODE": "true",
    }


def run_build(target: str) -> int:
    env = demo_env() if target == "demo" else None
    if target == "search-index":
        return run_command(["python3", str(ROOT / "pipeline" / "export_search_index.py")], env=env)
    if target == "preview-index":
        return run_command(["python3", str(ROOT / "pipeline" / "export_preview_index.py")], env=env)
    return run_command(["npm", "run", "build"], cwd=WEB_DIR, env=env)
