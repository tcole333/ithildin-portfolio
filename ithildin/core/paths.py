"""Shared path and environment configuration for Ithildin runtime code."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _path_from_env(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if value:
        return Path(value).expanduser().resolve()
    return default.resolve()


def content_root() -> Path:
    return _path_from_env("ITHILDIN_CONTENT_ROOT", repo_root() / "content")


def pipeline_root() -> Path:
    return _path_from_env("ITHILDIN_PIPELINE_ROOT", repo_root() / "pipeline")


def investigation_db_path() -> Path:
    return _path_from_env("ITHILDIN_INVESTIGATION_DB", repo_root() / "investigation.db")


def registry_db_path() -> Path:
    return _path_from_env("ITHILDIN_REGISTRY_DB", repo_root() / "registry.db")


def doj_db_path() -> Path:
    return _path_from_env("ITHILDIN_DOJ_DB", repo_root() / "doj_documents.db")


def email_archive_root() -> Path:
    return _path_from_env(
        "ITHILDIN_EMAIL_ARCHIVE_ROOT",
        repo_root() / "datasets" / "epstein-archive" / "data" / "emails",
    )


def workdir_base() -> Path:
    return _path_from_env(
        "OSINT_WORKDIR_BASE",
        Path(tempfile.gettempdir()) / "osint-jobs",
    )


def portfolio_demo_root() -> Path:
    return repo_root() / "examples" / "portfolio-demo"


def portfolio_demo_content_root() -> Path:
    return portfolio_demo_root() / "content"


def portfolio_demo_investigation_db() -> Path:
    return portfolio_demo_root() / "investigation.db"


def portfolio_demo_doj_db() -> Path:
    return portfolio_demo_root() / "doj_documents.db"


def portfolio_demo_registry_db() -> Path:
    return portfolio_demo_root() / "registry.db"
