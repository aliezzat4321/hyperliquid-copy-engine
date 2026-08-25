# Third-Party Wallet Identifier

The production path is deliberately source-agnostic:

1. A read-only third-party adapter collects immutable, closed-trade evidence.
2. Evidence is normalized to the generic closed-trades contract.
3. Independent trade aliases are canonicalized before the evidence threshold is counted.
4. Ready portfolios are resolved against free SQD finalized Hyperliquid fills.
5. Discovery matches are verified on disjoint, full position lifecycles.
6. Only a unique held-out winner is recorded as `VERIFIED`.

Invo is the first adapter. Carmine and Bones are processed first, followed by the rest of
the ready Invo queue. Adding another third party should require a new read-only collector
that emits the same resolver CSV, not another wallet-matching engine.

## Two-tier identity proof

The identifier chooses the strongest proof mode supported by the source evidence; it never
converts an allocation percentage into a fake contract quantity.

### Tier A — absolute-size proof

When every source trade contains a trustworthy absolute position size, the existing strict
resolver is used unchanged. Discovery and held-out verification require size agreement in
addition to coin, direction, time, price, flat-to-open boundary reconstruction, complete
position lifecycle replay and a unique held-out winner.

### Tier B — size-agnostic sequence proof

Sources such as Invo currently expose `entrySize` as an allocation percentage, not an
absolute Hyperliquid quantity. Those rows use a separate stronger sequence gate instead of
being permanently `UNRESOLVED` or weakening the Tier A matcher:

- at least 20 independent closed trades are required;
- eight source-selected discovery anchors are queried against SQD;
- a discovery vote can come only from a `startPosition`-proven final flatten matching the
  source coin, direction, close time and close price;
- at least four discovery anchors must identify the same candidate, with bounded close-clock
  dispersion and price error;
- discovery is candidate generation only and can never verify a wallet;
- twelve disjoint held-out trades are replayed against candidate-specific SQD fills;
- held-out matches must reconstruct a complete flat-to-open-to-flat lifecycle, including
  scale-ins and partial reductions, with continuous `startPosition`, entry/exit price and
  time agreement, exact internal size balance and stable source-vs-Hyperliquid close-clock
  offset;
- discovery executions and held-out lifecycles cannot be reused;
- verification requires at least five of twelve held-out matches, at least a 40% held-out
  match ratio, and a two-match lead over the strongest runner-up;
- more than six discovery finalists fails closed rather than widening verification work;
- mixed absolute-size and size-unknown evidence fails closed instead of switching proof
  semantics inside one identity run.

These requirements are intentionally stronger than the minimum Tier A evidence counts because
Tier B does not have source quantity as an identity feature.

## Continuous flow

`hyperliquid-invo-source-miner.timer` runs the read-only collector every five minutes. A
successful collection triggers `hyperliquid-invo-wallet-identifier.service`. The identifier:

- reuses one SQD client and its coverage/header cache across the batch;
- skips unchanged verified evidence and exponentially backs off unchanged unresolved evidence
  from one hour to a 24-hour maximum while still allowing SQD coverage to catch up;
- retries transport/runtime errors on the next successful collector cycle;
- processes at most four changed portfolios per run;
- never promotes a wallet to validation, shadow, or live trading.

The miner and identifier hold the same systemd-managed runtime pipeline lock. A later timer cycle cannot replace
the queue or resolver CSVs while SQD resolution and identity publication are in progress.

Durable outputs are under `/var/lib/hyperliquid-copy-engine/invo`:

- `archive.sqlite3`: transactional, indexed event/evidence archive (legacy NDJSON is
  imported once without rewriting the full history each cycle);
- `resolution_queue/resolution_queue.json`: resolver-ready Invo portfolios;
- `identifier_state.json`: per-portfolio attempts and evidence hashes;
- `identified_wallets.json`: verified identities that are still present in the current
  resolver-ready queue;
- `wallet_identifications/<portfolio>/`: immutable resolver reports.

The resolver hashes and parses one immutable byte snapshot. Every accepted row must share
one explicit source identity, or the calling adapter must bind the file to an expected
identity; mixed or mismatched evidence fails closed.

## Bootstrap

Run once on the research VM:

```bash
cd /root/hyperliquid-copy-engine
sudo bash scripts/bootstrap_invo_source_miner.sh
```

The bootstrap validates the Invo credential before enabling the timer and restores the old
credential if replacement authentication fails. The wallet identifier refuses to run when
`REAL_TRADING_ENABLED=YES`.

## Current starting identities

Bones previously passed strict production acceptance as
`0x565590f4d2b00b567a564f56b13f898392aef180`. The current continuous service still requires
Bones to pass the current evidence digest and resolver-rule gate before publication. Carmine
remains the first Invo priority target and is published only if the applicable Tier A or Tier B
public-data identity proof returns a unique held-out winner.
