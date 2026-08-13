# Prospective path-truth activation

This collector exists only to establish causal margin metadata for future validation windows. Do not backfill old trades with a current margin table.

## Activate on the research host

After this branch is merged and the host has pulled `main`:

```bash
cd /root/hyperliquid-copy-engine
git pull --ff-only origin main
bash deploy/install_margin_snapshot_timer.sh
```

The installer immediately takes one official `meta` snapshot and then schedules a snapshot every 30 minutes. Records append to `data/research/margin_metadata.jsonl` with a local nanosecond receipt timestamp and the untouched upstream payload.

## Validation rule

A candidate path may begin no earlier than the first valid snapshot that covers its asset. `continuous_path_v2` remains fail-closed for missing/stale margin metadata, stale marks, missing funding, or liquidation failure. The collector refuses to run when `REAL_TRADING_ENABLED=YES`.

## Verify

```bash
systemctl status hlcopy-margin-snapshot.timer --no-pager
journalctl -u hlcopy-margin-snapshot.service -n 50 --no-pager
wc -l data/research/margin_metadata.jsonl
tail -n 1 data/research/margin_metadata.jsonl
```

Do not call any strategy a validated champion until the complete prospective path gate passes.
