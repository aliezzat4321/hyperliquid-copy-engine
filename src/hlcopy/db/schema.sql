CREATE TABLE IF NOT EXISTS raw_api_payloads (
    content_sha256 TEXT PRIMARY KEY,
    response_json JSONB NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_api_responses (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    request_json JSONB,
    response_json JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    content_sha256 TEXT NOT NULL
);
-- Post-normalization observation rows preserve request/time/hash identity while the
-- canonical response body lives once in raw_api_payloads. Do not add a large
-- observation-table hash index during storage recovery because the write path uses
-- the raw_api_payloads primary key instead.

CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    display_name TEXT,
    role TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
    snapshot_at TIMESTAMPTZ NOT NULL,
    address TEXT NOT NULL REFERENCES wallets(address),
    ranking_period TEXT NOT NULL,
    rank INTEGER,
    pnl NUMERIC,
    roi NUMERIC,
    volume NUMERIC,
    account_value NUMERIC,
    raw_json JSONB NOT NULL,
    PRIMARY KEY (snapshot_at, address, ranking_period)
);
CREATE INDEX IF NOT EXISTS idx_leaderboard_snapshots_address_time
    ON leaderboard_snapshots(address, snapshot_at DESC);

CREATE TABLE IF NOT EXISTS fills (
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    tid BIGINT NOT NULL,
    oid BIGINT,
    tx_hash TEXT,
    timestamp TIMESTAMPTZ NOT NULL,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,
    direction TEXT,
    price NUMERIC NOT NULL,
    size NUMERIC NOT NULL,
    start_position NUMERIC NOT NULL,
    closed_pnl NUMERIC NOT NULL,
    fee NUMERIC NOT NULL,
    fee_token TEXT,
    crossed BOOLEAN,
    builder_fee NUMERIC NOT NULL DEFAULT 0,
    raw_json JSONB NOT NULL,
    PRIMARY KEY (wallet_address, tid)
);
CREATE INDEX IF NOT EXISTS idx_fills_wallet_time ON fills(wallet_address, timestamp);
CREATE INDEX IF NOT EXISTS idx_fills_coin_time ON fills(coin, timestamp);

CREATE TABLE IF NOT EXISTS orders (
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    oid BIGINT NOT NULL,
    cloid TEXT,
    timestamp TIMESTAMPTZ,
    coin TEXT,
    side TEXT,
    limit_price NUMERIC,
    size NUMERIC,
    original_size NUMERIC,
    order_type TEXT,
    tif TEXT,
    reduce_only BOOLEAN,
    status TEXT,
    status_timestamp TIMESTAMPTZ,
    raw_json JSONB NOT NULL,
    PRIMARY KEY (wallet_address, oid)
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    timestamp TIMESTAMPTZ NOT NULL,
    ranking_window TEXT NOT NULL,
    account_value NUMERIC,
    pnl NUMERIC,
    volume NUMERIC,
    raw_json JSONB NOT NULL,
    PRIMARY KEY (wallet_address, timestamp, ranking_window)
);

CREATE TABLE IF NOT EXISTS position_episodes (
    id BIGSERIAL PRIMARY KEY,
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    coin TEXT NOT NULL,
    direction TEXT NOT NULL,
    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    avg_entry NUMERIC,
    avg_exit NUMERIC,
    max_size NUMERIC NOT NULL,
    realized_pnl NUMERIC NOT NULL,
    fees NUMERIC NOT NULL,
    funding NUMERIC NOT NULL DEFAULT 0,
    holding_seconds DOUBLE PRECISION,
    complete_start BOOLEAN NOT NULL,
    fill_count INTEGER NOT NULL,
    fill_tids BIGINT[] NOT NULL
);
ALTER TABLE position_episodes
    DROP CONSTRAINT IF EXISTS position_episodes_wallet_address_coin_fill_tids_key;
CREATE INDEX IF NOT EXISTS idx_position_episodes_wallet_coin_opened
    ON position_episodes(wallet_address, coin, opened_at);

CREATE TABLE IF NOT EXISTS market_snapshots (
    timestamp TIMESTAMPTZ NOT NULL,
    coin TEXT NOT NULL,
    bid NUMERIC,
    ask NUMERIC,
    mid NUMERIC,
    mark NUMERIC,
    spread_bps DOUBLE PRECISION,
    depth_json JSONB,
    raw_json JSONB NOT NULL,
    PRIMARY KEY (timestamp, coin)
);

CREATE TABLE IF NOT EXISTS wallet_metrics (
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    as_of_timestamp TIMESTAMPTZ NOT NULL,
    lookback TEXT NOT NULL,
    metrics_json JSONB NOT NULL,
    PRIMARY KEY (wallet_address, as_of_timestamp, lookback)
);

CREATE TABLE IF NOT EXISTS trader_profiles (
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    as_of_timestamp TIMESTAMPTZ NOT NULL,
    lookback_start TIMESTAMPTZ NOT NULL,
    model_version TEXT NOT NULL,
    profile_json JSONB NOT NULL,
    PRIMARY KEY (wallet_address, as_of_timestamp, model_version)
);
CREATE INDEX IF NOT EXISTS idx_trader_profiles_wallet_time
    ON trader_profiles(wallet_address, as_of_timestamp DESC);

CREATE TABLE IF NOT EXISTS copyability_runs (
    id BIGSERIAL PRIMARY KEY,
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    model_version TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    latency_ms INTEGER NOT NULL,
    capital NUMERIC NOT NULL,
    net_pnl NUMERIC,
    roi NUMERIC,
    max_drawdown NUMERIC,
    fills_copied INTEGER,
    fills_missed INTEGER,
    avg_slippage_bps DOUBLE PRECISION,
    result_json JSONB NOT NULL
);
