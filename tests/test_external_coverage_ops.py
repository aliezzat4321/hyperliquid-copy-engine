from pathlib import Path


def test_external_coverage_does_not_full_upsert_leaderboard() -> None:
    source = Path("src/hlcopy/research/coverage.py").read_text(encoding="utf-8")
    assert "await db.upsert_leaderboard(" not in source
    assert "await db.ensure_leaderboard_wallet(" in source


def test_external_coverage_service_uses_shared_lock_and_larger_batch() -> None:
    service = Path("deploy/systemd/hyperliquid-external-coverage.service").read_text(
        encoding="utf-8"
    )
    assert "/run/hyperliquid-rest-research.lock" in service
    assert "--batch-size 100" in service
    assert "REAL_TRADING_ENABLED=NO" in service


def test_wallet_research_uses_same_rest_lock() -> None:
    service = Path("deploy/systemd/hyperliquid-wallet-research.service").read_text(
        encoding="utf-8"
    )
    assert "/run/hyperliquid-rest-research.lock" in service


def test_external_coverage_timer_runs_every_thirty_minutes() -> None:
    timer = Path("deploy/systemd/hyperliquid-external-coverage.timer").read_text(
        encoding="utf-8"
    )
    assert "OnUnitActiveSec=30min" in timer


def test_external_resolver_scores_full_five_thousand_candidate_universe() -> None:
    service = Path("deploy/systemd/hyperliquid-external-resolver.service").read_text(
        encoding="utf-8"
    )
    source = Path("src/hlcopy/resolver/engine.py").read_text(encoding="utf-8")
    assert "--max-candidates 5000" in service
    assert "max_candidates: int = 5_000" in source


def test_external_resolver_avoids_one_query_per_candidate_wallet() -> None:
    source = Path("src/hlcopy/resolver/engine.py").read_text(encoding="utf-8")
    assert "WITH selected AS" in source
    assert "JOIN selected AS s" in source
    assert "for address in addresses:" not in source
