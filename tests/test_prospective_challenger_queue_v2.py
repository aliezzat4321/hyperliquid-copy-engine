import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prospective_champion_lane.py"
FUNNEL = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "hlcopy"
    / "profitability"
    / "incremental_funnel_cli.py"
)
SPEC = importlib.util.spec_from_file_location("prospective_champion_lane", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_load_frozen_targets_uses_only_selective_challengers(tmp_path: Path) -> None:
    queue = tmp_path / "queue.json"
    queue.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "status": "challenger",
                        "wallet_address": "0x" + "A" * 40,
                        "coin": "SOL",
                        "notional_usd": "1000",
                        "prospective_start_ns": 42,
                        "candidate_key": "key-a",
                    },
                    {
                        "status": "demoted",
                        "wallet_address": "0x" + "b" * 40,
                        "coin": "BTC",
                        "notional_usd": "5000",
                        "prospective_start_ns": 9,
                        "candidate_key": "key-b",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    assert MODULE.load_frozen_targets(queue) == [
        {
            "wallet": "0x" + "a" * 40,
            "coin": "SOL",
            "primary_notional": "1000",
            "prospective_start_ns": 42,
            "candidate_key": "key-a",
        }
    ]


def test_prospective_script_has_no_hard_coded_wallet_targets() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "TARGETS=" not in source
    assert "load_frozen_targets" in source


def test_funnel_does_not_truncate_robust_candidates_before_challenger_handoff() -> None:
    source = FUNNEL.read_text(encoding="utf-8")
    assert "build_challenger_queue(\n        robust," in source
    assert "build_challenger_queue(\n        robust[:100]," not in source
