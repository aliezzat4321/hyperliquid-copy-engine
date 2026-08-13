import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from hlcopy.resolver.identifier import identify_wallet_from_csv

D = Decimal
EXPECTED = "0x565590f4d2b00b567a564f56b13f898392aef180"
FIXTURE = Path("tmp/bones_acceptance_live_v10.csv")


def test_bones_live_acceptance_temp(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = asyncio.run(identify_wallet_from_csv(FIXTURE, output_dir=out))
    report_path = out / "wallet_identification_bones_acceptance_live_v10.json"
    report = json.loads(report_path.read_text())

    assert result.status == "VERIFIED", report
    assert result.wallet == EXPECTED, report
    assert report["candidate_unique"] is True, report
    assert report["uncovered_signal_ids"] == [], report
    assert report["input_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), report

    rows = sorted(
        report["historical_candidate_verifications"],
        key=lambda row: (
            row["verification"]["matched"],
            D(row["verification"]["ratio"]),
        ),
        reverse=True,
    )
    assert rows and rows[0]["address"] == EXPECTED, rows
    winner = rows[0]["verification"]
    runner = rows[1]["verification"]["matched"] if len(rows) > 1 else 0
    cfg = report["effective_config"]
    assert winner["matched"] >= int(cfg["min_historical_matches"]), rows
    assert D(winner["ratio"]) >= D(cfg["min_historical_ratio"]), rows
    assert winner["matched"] - runner >= int(cfg["min_historical_winner_match_gap"]), rows

    matched = [e for e in winner["evidence"] if e["matched"]]
    assert matched
    assert all(e["lifecycle_id"] and e["final_execution_id"] for e in matched)
    assert len({e["lifecycle_id"] for e in matched}) == len(matched)

    discovery = report["winning_discovery_candidate"]
    discovery_exec = {
        match["trade_id"].removeprefix("final-flatten:")
        for match in discovery["matches"]
        if match["trade_id"].startswith("final-flatten:")
    }
    held_exec = {e["final_execution_id"] for e in matched}
    assert discovery_exec.isdisjoint(held_exec), (discovery_exec, held_exec)

    safety = report["safety"]
    assert safety["discovery_held_out_execution_disjointness_required"] is True
    assert safety["overlapping_source_positions_collapsed"] is True
    assert safety["exact_boundary_sequence_replay_required"] is True
    assert safety["one_vote_per_sqd_execution_in_discovery"] is True
    assert safety["one_vote_per_sqd_lifecycle_in_verification"] is True

    print("BONES_STRICT_VERIFIED", EXPECTED)
    print("INDEPENDENT_UNITS", report["accepted_trades"])
    print("OVERLAPS_COLLAPSED", len(report["overlapping_rows"]))
    print("WINNER", winner["matched"], "/", winner["attempted"])
    print("RUNNER", runner)
