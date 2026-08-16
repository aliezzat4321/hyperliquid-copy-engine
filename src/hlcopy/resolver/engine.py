from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from hlcopy.resolver.matcher import (
    CandidateResolution,
    candidate_events,
    candidate_universe_fingerprint,
    decide_resolution,
    evidence_events,
    evidence_fingerprint,
    score_candidate,
    select_anchor_trades,
)
from hlcopy.resolver.source_registry import ExternalSourceSpec
from hlcopy.shadow.registry import WalletRegistry, WalletSpec
from hlcopy.signals.invo import CopySignal, load_invo_closed_trades

D = Decimal


@dataclass(frozen=True, slots=True)
class ResolverConfig:
    anchor_trades: int = 16
    evidence_lookback_days: int = 14
    time_tolerance_ms: int = 5_000
    price_tolerance_bps: Decimal = D("5")
    max_candidates: int = 5_000
    report_candidates: int = 25


DEFAULT_RESOLVER_CONFIG = ResolverConfig()


@dataclass(frozen=True, slots=True)
class ResolverRun:
    source_id: str
    status: str
    verified_address: str | None
    evidence_trades: int
    evidence_events: int
    candidate_wallets: int
    evidence_fingerprint: str
    candidate_universe_fingerprint: str
    evidence_file_sha256: str
    report_path: str


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_source_signals(source: ExternalSourceSpec) -> tuple[CopySignal, ...]:
    path = Path(source.evidence_path)
    if source.adapter == "invo_closed_trades_csv":
        result = load_invo_closed_trades(path)
        if result.rejected_rows:
            raise ValueError(
                f"external evidence contains {len(result.rejected_rows)} malformed rows; "
                "resolver fails closed"
            )
        return result.signals
    raise ValueError(f"unsupported external adapter: {source.adapter}")


def _recent_signals(signals: tuple[CopySignal, ...], lookback_days: int) -> tuple[CopySignal, ...]:
    if not signals:
        return ()
    newest_ms = max(signal.closed_at_ms for signal in signals)
    cutoff_ms = newest_ms - int(timedelta(days=max(1, lookback_days)).total_seconds() * 1000)
    recent = tuple(signal for signal in signals if signal.closed_at_ms >= cutoff_ms)
    if len(recent) >= 6:
        return recent
    return tuple(sorted(signals, key=lambda item: item.closed_at_ms)[-16:])


async def _candidate_resolutions(
    dsn: str,
    *,
    start_ms: int,
    end_ms: int,
    max_candidates: int,
    evidence: tuple[Any, ...],
    time_tolerance_ms: int,
    price_tolerance_bps: Decimal,
) -> tuple[tuple[str, ...], tuple[CandidateResolution, ...]]:
    """Score candidate wallets while streaming DB rows one wallet at a time.

    The old resolver used ``fetchall()`` and retained every selected wallet's raw
    fill JSON until the full candidate universe had loaded. A 5k-wallet run can
    therefore exceed the memory available on the research VM. The SQL ordering
    already groups rows by wallet, so only one wallet's raw rows need to exist in
    memory at once.
    """
    start = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
    addresses: list[str] = []
    ranked: list[CandidateResolution] = []

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        cursor = await conn.execute(
            """
            WITH selected AS (
                SELECT wallet_address, COUNT(*) AS n, MAX(timestamp) AS latest
                FROM fills
                WHERE timestamp BETWEEN %s AND %s
                GROUP BY wallet_address
                ORDER BY n DESC, latest DESC
                LIMIT %s
            )
            SELECT f.wallet_address, f.raw_json
            FROM fills AS f
            JOIN selected AS s
              ON s.wallet_address = f.wallet_address
            WHERE f.timestamp BETWEEN %s AND %s
            ORDER BY f.wallet_address, f.timestamp, f.tid
            """,
            (start, end, max_candidates, start, end),
        )

        current_address: str | None = None
        current_rows: list[dict[str, Any]] = []

        def flush() -> None:
            nonlocal current_address, current_rows
            if current_address is None:
                return
            addresses.append(current_address)
            ranked.append(
                score_candidate(
                    address=current_address,
                    evidence=evidence,
                    candidate=candidate_events(current_address, current_rows),
                    time_tolerance_ms=time_tolerance_ms,
                    price_tolerance_bps=price_tolerance_bps,
                )
            )
            current_rows = []

        async for row in cursor:
            address = str(row[0]).lower()
            if current_address is None:
                current_address = address
            elif address != current_address:
                flush()
                current_address = address

            raw = row[1]
            if isinstance(raw, dict):
                current_rows.append(raw)

        flush()

    return (
        tuple(addresses),
        tuple(
            sorted(
                ranked,
                key=lambda item: (item.score, item.matched_events, item.address),
                reverse=True,
            )
        ),
    )


def _ensure_verified_wallet(
    *,
    wallet_registry: WalletRegistry,
    source: ExternalSourceSpec,
    address: str,
    coins: tuple[str, ...],
    report_fingerprint: str,
) -> None:
    wallet_registry.init()
    existing = wallet_registry.load()
    if any(
        wallet.source_type == "hyperliquid_wallet"
        and wallet.source_ref.lower() == address.lower()
        for wallet in existing
    ):
        return
    suffix = address.lower().removeprefix("0x")[:10]
    wallet_registry.add(
        WalletSpec(
            id=f"resolved-{source.id}-{suffix}",
            label=f"{source.label} (resolved)",
            source_type="hyperliquid_wallet",
            source_ref=address.lower(),
            stage="research",
            coins=coins,
            notes=(
                f"Strict external identity resolution from {source.id}; "
                f"report_fingerprint={report_fingerprint[:16]}; "
                "RESEARCH only; human promotion to validation required"
            ),
        )
    )


def _candidate_summary(candidate: CandidateResolution) -> dict[str, object]:
    return candidate.to_dict()


async def resolve_source(
    *,
    source: ExternalSourceSpec,
    database_url: str,
    wallet_registry: WalletRegistry,
    output_dir: Path,
    config: ResolverConfig = DEFAULT_RESOLVER_CONFIG,
) -> ResolverRun:
    evidence_path = Path(source.evidence_path)
    if not evidence_path.is_file():
        raise FileNotFoundError(evidence_path)
    all_signals = _load_source_signals(source)
    recent = _recent_signals(all_signals, config.evidence_lookback_days)
    anchors = select_anchor_trades(recent, max_trades=config.anchor_trades)
    events = evidence_events(anchors)
    if len(anchors) < 6 or len(events) < 12:
        raise ValueError("insufficient independent external evidence for identity resolution")

    start_ms = min(event.timestamp_ms for event in events) - config.time_tolerance_ms
    end_ms = max(event.timestamp_ms for event in events) + config.time_tolerance_ms
    addresses, ranked = await _candidate_resolutions(
        database_url,
        start_ms=start_ms,
        end_ms=end_ms,
        max_candidates=config.max_candidates,
        evidence=events,
        time_tolerance_ms=config.time_tolerance_ms,
        price_tolerance_bps=config.price_tolerance_bps,
    )
    decision = decide_resolution(ranked)
    evidence_fp = evidence_fingerprint(events)
    universe_fp = candidate_universe_fingerprint(addresses)
    evidence_file_sha = _file_sha256(evidence_path)
    payload: dict[str, object] = {
        "version": 1,
        "resolver_rule_version": "external-wallet-identity-v1",
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "source": source.to_dict(),
        "config": {
            **asdict(config),
            "price_tolerance_bps": str(config.price_tolerance_bps),
        },
        "evidence": {
            "file_sha256": evidence_file_sha,
            "all_signal_count": len(all_signals),
            "recent_signal_count": len(recent),
            "anchor_trade_count": len(anchors),
            "anchor_event_count": len(events),
            "fingerprint": evidence_fp,
            "anchor_trade_ids": [signal.signal_id for signal in anchors],
        },
        "candidate_universe": {
            "count": len(addresses),
            "fingerprint": universe_fp,
            "source": "locally persisted point-in-time Hyperliquid research fills",
        },
        "decision": {
            "status": decision.status,
            "verified_address": decision.verified_address,
            "reason_codes": list(decision.reason_codes),
            "best_score": str(decision.best_score) if decision.best_score is not None else None,
            "runner_up_score": (
                str(decision.runner_up_score) if decision.runner_up_score is not None else None
            ),
            "score_gap": str(decision.score_gap) if decision.score_gap is not None else None,
        },
        "ranked_candidates": [
            _candidate_summary(candidate) for candidate in ranked[: config.report_candidates]
        ],
        "safety": {
            "auto_validation_promotion": False,
            "auto_trading_promotion": False,
            "private_source_scraping": False,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    report_fp = hashlib.sha256(canonical.encode()).hexdigest()
    payload["report_fingerprint"] = report_fp
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = output_dir / f"external_resolution_{source.id}_{stamp}.json"
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if decision.status == "VERIFIED" and decision.verified_address is not None:
        coins = tuple(sorted({signal.coin for signal in anchors}))
        _ensure_verified_wallet(
            wallet_registry=wallet_registry,
            source=source,
            address=decision.verified_address,
            coins=coins,
            report_fingerprint=report_fp,
        )

    return ResolverRun(
        source_id=source.id,
        status=decision.status,
        verified_address=decision.verified_address,
        evidence_trades=len(anchors),
        evidence_events=len(events),
        candidate_wallets=len(addresses),
        evidence_fingerprint=evidence_fp,
        candidate_universe_fingerprint=universe_fp,
        evidence_file_sha256=evidence_file_sha,
        report_path=str(report_path),
    )
