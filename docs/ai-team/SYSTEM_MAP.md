# System Map

Durable component map. Its purpose is **token efficiency**: locate the right code
without rediscovering 130 modules. Read the rows for your lane, not the repository.

Update this file only when a component moves, is added, or is retired.

## Shared infrastructure

| Concern | Path | Notes |
|---|---|---|
| Hyperliquid HTTP | `src/hlcopy/hyperliquid/http_client.py` | Weighted rate limiter, retries |
| Hyperliquid WebSocket | `src/hlcopy/hyperliquid/websocket.py` | Heartbeat, reconnect/backoff |
| Market tape capture | `src/hlcopy/market/capture.py`, `tape.py`, `normalize.py` | BBO / L2 / trades / asset ctx to Parquet |
| Historical archive | `src/hlcopy/market/historical_archive.py` | Hyperliquid published archives |
| Symbols | `src/hlcopy/market/symbols.py` | `canonical_coin` / `wire_coin` |
| Position reconstruction | `src/hlcopy/positions/state_machine.py`, `reconstruction.py` | `startPosition` invariant; incomplete-start episodes excluded from scoring |
| Executable fill model | `src/hlcopy/copyability/slippage.py` | Book-walk VWAP with a slippage cap; refuses to invent liquidity |
| Profitability evidence auditor | `src/hlcopy/profitability/evidence_auditor.py`, `evidence_audit_cli.py` | Reusable fail-closed ledger/provenance/economics contract; Lane 3 JSONL adapter |
| Latency scenarios | `src/hlcopy/copyability/latency.py`, `src/hlcopy/shadow/latency.py` | Feed vs order latency kept separate |
| Wallet registry | `src/hlcopy/shadow/registry.py` | `research → validation → approved`; enforces the 10-user-per-IP cap |
| Trading permission boundary | `src/hlcopy/trading/permissions.py` | `REAL_TRADING_ENABLED`; **live-sensitive** |
| Risk eligibility ceiling | `src/hlcopy/risk/governor.py` | Deterministic candidate state; never authorizes real trading |
| Config | `src/hlcopy/config.py` | `HLCOPY_*` settings |
| Experiment freeze/audit | `src/hlcopy/research/experiment_controller.py` | Fingerprints ex-ante contracts; fail-closed promotion-report verdict |
| Storage pressure controller | `scripts/storage_controller.py`, `config/storage_policy.json` | Read-only fail-closed budgets, forecast and writer decisions |
| Storage exit-gate evaluator | `scripts/storage_exit_gate_report.py` | Read-only conjunction over reviewed apply, policy and uncontaminated controller evidence |
| Market tape lifecycle | `scripts/market_tape_lifecycle.py`, `config/market_tape_lifecycle.json` | Exact-SHA lossless historical Parquet compaction; no deletion without review |
| Database | `src/hlcopy/db/postgres.py`, `db/schema.sql` | Append-only raw + derived tables |
| CLI | `src/hlcopy/cli.py` | `hlcopy` entry point |

## Lane 1 — Hyperliquid native discovery

| Stage | Path | Runtime |
|---|---|---|
| Leaderboard / universe discovery | `src/hlcopy/discovery/leaderboard.py`, `universe.py`, `universe_watch_cli.py` | `hyperliquid-universe-scout.timer` |
| Wide public-trade watch | `src/hlcopy/shadow/wide_watch.py`, `wide_live.py` | `hyperliquid-wide-trade-watch{,-live}.service` |
| Fill enrichment | `src/hlcopy/shadow/wide_enrich*.py` | `hyperliquid-wide-fill-enrichment{,-live}.service` |
| Wide scoring | `src/hlcopy/shadow/wide_score.py`, `wide_score_cli.py` | `hyperliquid-wide-live-scoreboard.timer` |
| Execution-realistic replay | `src/hlcopy/profitability/position_copy.py`, `portfolio_position_copy.py`, `causal_book.py` | — |
| Path truth / streaming | `src/hlcopy/profitability/path_truth.py`, `streaming_path_truth.py`, `parquet_stream_evaluator.py` | — |
| Research funnel | `src/hlcopy/profitability/incremental_funnel_cli.py`, `max_profitability.py` | `hyperliquid-profitability.timer` |
| Selective challenger handoff / frozen prospective evaluation | `src/hlcopy/profitability/lane1_handoff.py`, `scripts/prospective_champion_lane.py` | Robust wallet×coin candidates are freshness-gated against the official leaderboard and receive immutable per-candidate prospective cutoffs |
| Trader forensics | `src/hlcopy/profiling.py`, `src/hlcopy/analytics/trader_profile.py`, `performance.py` | `hyperliquid-wallet-research.timer` |
| Cohort / policy | `src/hlcopy/research/cohort.py`, `selective_policy_publisher.py` | `hyperliquid-validation-cohort.service` |

## Lane 2 — Third-party identity resolution

| Stage | Path | Runtime |
|---|---|---|
| Invo read-only collector | `src/hlcopy/discovery/invo_source.py`, `invo_miner_job.py` | `hyperliquid-invo-source-miner.timer` |
| Universe / screening | `src/hlcopy/discovery/invo_universe_job.py` | `hyperliquid-invo-universe-miner.timer` |
| Evidence + queue | `src/hlcopy/discovery/invo_evidence.py`, `invo_resolution_queue.py`, `invo_store.py` | — |
| Direct history | `src/hlcopy/discovery/invo_direct_history_job.py` | `hyperliquid-invo-direct-history.service` |
| Identity resolution | `src/hlcopy/discovery/invo_identifier_job.py`, `invo_durable_identity.py` | `hyperliquid-invo-wallet-identifier.service` |
| Tier A strict matcher | `src/hlcopy/resolver/identifier.py`, `matcher.py`, `engine.py` | — |
| Tier B size-agnostic | `src/hlcopy/resolver/size_agnostic_identifier.py` | Seven-clause gate; 0 published identities |
| SQD fill access | `src/hlcopy/resolver/sqd_fills.py`, `sqd_position_aware.py` | — |
| Reverse / trade indexes | `src/hlcopy/resolver/reverse_index.py`, `public_trade_index.py` | `hyperliquid-external-reverse-resolver.service` |
| Source registry | `src/hlcopy/resolver/source_registry.py` | Source-agnostic CSV contract |
| Shadow handoff | `src/hlcopy/discovery/verified_identity_shadow_sync.py` | `hyperliquid-invo-verified-shadow-sync.service` |
| Third-party scoring | `src/hlcopy/third_party/profitability_cli.py`, `registry_sync.py` | `hyperliquid-third-party-profitability.timer` |

## Lane 3 — Direct Invo notification copying

| Stage | Path | Runtime |
|---|---|---|
| Executor service (TypeScript) | `services/invo-notification-executor/src/service.ts` | `hyperliquid-invo-notification-executor.service` |
| Signal parsing | `services/invo-notification-executor/src/notification-signal.ts` | `verifiedTrade` gate, open/increase/close |
| Ownership state | `services/invo-notification-executor/src/notification-state.ts` | Persists across restarts |
| Trader discovery / lifecycle | `services/invo-notification-executor/src/trader-tracker.ts` | PnL-blind multi-surface funnel and shadow-assessment queue |
| Invo API client | `services/invo-notification-executor/src/invo-client.ts` | `/v1_0/posts/get_feed`; **live-sensitive** (`/dex/position/*`) |
| Hyperliquid client | `services/invo-notification-executor/src/hl-client.ts` | **live-sensitive** (order placement) |
| Signal import (Python) | `src/hlcopy/signals/invo.py`, `generic_csv.py` | — |
| Net executable profitability ledger | `src/hlcopy/lane3/`, `scripts/lane3_net_edge_report.py` | Offline, fail-closed Phase A/B2 measurement; no live permissions |

## Data stores

| Store | Location | Contents |
|---|---|---|
| PostgreSQL | `DATABASE_URL` | `raw_api_responses`, `fills`, `orders`, `position_episodes`, `trader_profiles`, `copyability_runs` |
| Market tape | `/mnt/HC_Volume_106576526/hyperliquid/market-shadow` | Partitioned Parquet; **volume at 100%, Issue #90** |
| Wide evidence | `/mnt/HC_Volume_106576526/hyperliquid/shadow/wide-enriched-live` | JSONL public-fill evidence |
| Wallet registry | `/mnt/HC_Volume_106576526/hyperliquid/shadow/wallets.json` | Stage machine state |
| Invo durable state | `/var/lib/hyperliquid-copy-engine/invo/` | `archive.sqlite3`, `resolution_queue/`, `identified_wallets.json` |
| Lane 3 ledger | `/var/lib/hyperliquid-copy-engine/invo-notification-executor/audit.jsonl` | Append-only decision stream |
| Frozen champion report | `/root/hyperliquid-audit/prospective-champions/report.json` | Lane 1 frozen window |

## Observability workflows

These already print the numbers that belong in `state.json`. Cite their run IDs as
provenance rather than re-deriving values.

| Workflow | Cadence | Publishes |
|---|---|---|
| `edge-observability-watchdog.yml` | 15 min | Identity yield, queue depth, storage, freshness |
| `invo-notification-executor-status.yml` | 15 min | Lane 3 ledger, latency, chase, skip reasons |
| `profitability-funnel-status.yml` | ~2 h | Lane 1 funnel counts, disk |
| `third-party-scorecard.yml` | ~2 h | Third-party wallet and event counts |
| `prospective-champion-status.yml` | 15 min | Frozen target approvals |
| `storage-governance.yml` | hourly | Postgres audit, disk guard install |

## Canonical docs

| Topic | Path |
|---|---|
| Agent constitution | `AGENTS.md` |
| Current state (generated) | `docs/ai-team/CURRENT_STATE.md` |
| Profitability evidence rules | `docs/ai-team/PROFITABILITY_STANDARD.md` |
| Promotion thresholds | `docs/ai-team/PROMOTION_POLICY.md` |
| Live capital gate | `docs/ai-team/LIVE_TRADING_GATE.md` |
| Review provenance limits | `docs/ai-team/REVIEW_PROVENANCE.md` |
| Shadow architecture | `docs/SHADOW_ARCHITECTURE.md` |
| Resolver design | `docs/WALLET_IDENTIFIER.md`, `docs/RESOLVER_SCANNING.md` |
| Lane 3 net edge | `docs/INVO_NOTIFICATION_NET_EDGE.md` (arrives with PR #95) |
| Market tape contract | `docs/market_tape.md` |
| Storage exit gate | `docs/ai-team/STORAGE_EXIT_GATE.md` |
| Trader forensics | `docs/trader_forensics.md` |
