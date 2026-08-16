Rollout checklist:

1. CI must pass Ruff and full pytest.
2. Install the updated systemd unit from the merged commit.
3. Stop the currently wedged selective-shadow oneshot; do not delete any evidence artifacts.
4. Restart the timer and start one fresh selective-shadow run.
5. Confirm `selective_path_truth_plan` reports mature/deferred counts.
6. Confirm mature progress advances and the service exits successfully within the configured timeout.
7. Confirm `path_truth.json` policy id matches the latest effective policy after a completed cycle.
8. Confirm `forward_vetoes.json` refreshes after path truth.
9. Keep `REAL_TRADING_ENABLED=NO`.
