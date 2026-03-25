from __future__ import annotations

import ithildin.cli as cli


def test_cli_dispatches_tracker_command(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_tracker(args: list[str]) -> int:
        captured["args"] = args
        return 7

    monkeypatch.setitem(cli.TRACKER_COMMANDS, "lead", fake_tracker)

    result = cli.main(["lead", "list", "--status", "open"])

    assert result == 7
    assert captured["args"] == ["list", "--status", "open"]


def test_cli_dispatches_query_provider(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_provider(args: list[str]) -> int:
        captured["args"] = args
        return 3

    monkeypatch.setitem(cli.QUERY_PROVIDERS, "registry", fake_provider)

    result = cli.main(["query", "registry", "search", "Harbor Ledger"])

    assert result == 3
    assert captured["args"] == ["search", "Harbor Ledger"]


def test_cli_dispatches_build_target(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_build(target: str) -> int:
        captured["target"] = target
        return 0

    monkeypatch.setattr(cli, "run_build", fake_build)

    result = cli.main(["build", "demo"])

    assert result == 0
    assert captured["target"] == "demo"
