# External Resolver Scanning

The external resolver has two phases:

1. `resolve` compares an external source fingerprint against fills already persisted by native wallet research.
2. `scan-resolve` expands the candidate universe by querying public Hyperliquid `userFillsByTime` in a narrow evidence window, caching the returned fills, checkpointing addresses already scanned, and rerunning the strict resolver.

The scan is intentionally bounded and resumable. It does not weaken identity thresholds, does not auto-promote a wallet to validation, and does not trade.

The production timer uses `scan-all` with a small batch so multiple external sources can progress without a large burst of public API traffic. The HTTP client retains the existing weighted Hyperliquid rate limiter and retry behavior.

For a manual source-specific pass:

```bash
python -m hlcopy.resolver.cli \
  --source-registry /mnt/HC_Volume_106576526/hyperliquid/resolver/sources.json \
  --wallet-registry /mnt/HC_Volume_106576526/hyperliquid/shadow/wallets.json \
  scan-resolve \
  --id bones \
  --output-dir /mnt/HC_Volume_106576526/hyperliquid/outputs/resolver \
  --anchor-trades 16 \
  --evidence-lookback-days 14 \
  --time-tolerance-ms 5000 \
  --price-tolerance-bps 5 \
  --max-candidates 500 \
  --report-candidates 25 \
  --batch-size 100 \
  --universe-limit 5000
```

The scan state is stored as `external_scan_state_<source>.json` in the resolver output directory. A result of `UNRESOLVED` means only that the strict identity gate was not satisfied within the candidate evidence accumulated so far.
