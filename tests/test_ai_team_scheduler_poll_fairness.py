import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "ai_team_router.json"
TIMER = ROOT / "deploy" / "systemd" / "hyperliquid-ai-team-orchestrator.timer"


def test_ci_repoll_window_leaves_cycles_for_fresh_pending_work() -> None:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    timer = TIMER.read_text(encoding="utf-8")
    match = re.search(r"(?m)^OnUnitActiveSec=(\d+)s$", timer)
    assert match is not None
    cycle_seconds = int(match.group(1))

    # due() intentionally polls WAITING_CI before PENDING. The CI re-poll window
    # therefore must be wider than one host timer interval so a fresh PENDING
    # assignment receives a dispatch cycle instead of being starved forever.
    assert int(cfg["poll_seconds"]) >= 2 * cycle_seconds
