# Prospective-only wide evidence lane

The original wide public trade watcher may receive subscription replay/backfill. Those rows are useful for gap recovery but must not be interpreted as low-latency prospective signals.

The `*-live.service` units form a clean evidence lane:

- public trades must occur after process start;
- public observed lag must be <= 2000 ms;
- official enrichment skips rows older than the enricher start and refuses to build a queue older than 10000 ms;
- all services remain shadow-only with `REAL_TRADING_ENABLED=NO`.

Historical/replay rows remain available in the original `wide-trades` path and are intentionally separate from `wide-trades-live`.
