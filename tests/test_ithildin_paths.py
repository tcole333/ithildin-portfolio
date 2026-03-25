from __future__ import annotations

from pathlib import Path

from ithildin.core.paths import (
    content_root,
    doj_db_path,
    investigation_db_path,
    pipeline_root,
    portfolio_demo_content_root,
    portfolio_demo_doj_db,
    portfolio_demo_investigation_db,
    portfolio_demo_registry_db,
    registry_db_path,
)


def test_repo_relative_defaults(monkeypatch, repo_root: Path) -> None:
    for name in (
        "ITHILDIN_CONTENT_ROOT",
        "ITHILDIN_PIPELINE_ROOT",
        "ITHILDIN_INVESTIGATION_DB",
        "ITHILDIN_REGISTRY_DB",
        "ITHILDIN_DOJ_DB",
    ):
        monkeypatch.delenv(name, raising=False)

    assert content_root() == (repo_root / "content").resolve()
    assert pipeline_root() == (repo_root / "pipeline").resolve()
    assert investigation_db_path() == (repo_root / "investigation.db").resolve()
    assert registry_db_path() == (repo_root / "registry.db").resolve()
    assert doj_db_path() == (repo_root / "doj_documents.db").resolve()


def test_env_overrides(monkeypatch, tmp_path: Path) -> None:
    overrides = {
        "ITHILDIN_CONTENT_ROOT": tmp_path / "custom-content",
        "ITHILDIN_PIPELINE_ROOT": tmp_path / "custom-pipeline",
        "ITHILDIN_INVESTIGATION_DB": tmp_path / "custom-investigation.db",
        "ITHILDIN_REGISTRY_DB": tmp_path / "custom-registry.db",
        "ITHILDIN_DOJ_DB": tmp_path / "custom-doj.db",
    }
    for name, path in overrides.items():
        monkeypatch.setenv(name, str(path))

    assert content_root() == overrides["ITHILDIN_CONTENT_ROOT"].resolve()
    assert pipeline_root() == overrides["ITHILDIN_PIPELINE_ROOT"].resolve()
    assert investigation_db_path() == overrides["ITHILDIN_INVESTIGATION_DB"].resolve()
    assert registry_db_path() == overrides["ITHILDIN_REGISTRY_DB"].resolve()
    assert doj_db_path() == overrides["ITHILDIN_DOJ_DB"].resolve()


def test_portfolio_demo_paths(repo_root: Path) -> None:
    expected_root = repo_root / "examples" / "portfolio-demo"
    assert portfolio_demo_content_root() == (expected_root / "content")
    assert portfolio_demo_investigation_db() == (expected_root / "investigation.db")
    assert portfolio_demo_registry_db() == (expected_root / "registry.db")
    assert portfolio_demo_doj_db() == (expected_root / "doj_documents.db")
