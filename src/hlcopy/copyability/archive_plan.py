from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hlcopy.signals.invo import CopySignal


@dataclass(frozen=True, slots=True, order=True)
class ArchiveObject:
    date: str
    hour: int
    coin: str

    @property
    def s3_uri(self) -> str:
        return (
            f"s3://hyperliquid-archive/market_data/"
            f"{self.date}/{self.hour}/l2Book/{self.coin}.lz4"
        )

    def local_path(self, root: Path) -> Path:
        return root / self.date / str(self.hour) / "l2Book" / self.coin


def required_l2_objects(
    signals: list[CopySignal] | tuple[CopySignal, ...],
    *,
    latencies_ms: list[int] | tuple[int, ...],
    book_lookback_ms: int = 1_000,
) -> tuple[ArchiveObject, ...]:
    objects: set[ArchiveObject] = set()
    for signal in signals:
        for latency_ms in latencies_ms:
            for base_ms in (signal.opened_at_ms, signal.closed_at_ms):
                target_ms = base_ms + latency_ms
                for lookup_ms in (target_ms, target_ms - book_lookback_ms):
                    dt = datetime.fromtimestamp(lookup_ms / 1000, tz=UTC)
                    objects.add(
                        ArchiveObject(
                            date=dt.strftime("%Y%m%d"),
                            hour=dt.hour,
                            coin=signal.coin.upper(),
                        )
                    )
    return tuple(sorted(objects))


def write_fetch_script(
    objects: list[ArchiveObject] | tuple[ArchiveObject, ...],
    *,
    root: Path,
    path: Path,
) -> Path:
    """Write an explicit requester-pays fetch script; never downloads implicitly."""
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        'command -v aws >/dev/null || { echo "aws CLI is required" >&2; exit 1; }',
        'command -v lz4 >/dev/null || { echo "lz4 is required" >&2; exit 1; }',
        "",
    ]
    for obj in objects:
        target = obj.local_path(root)
        compressed = Path(str(target) + ".lz4")
        lines.extend(
            [
                f"mkdir -p {shlex.quote(str(target.parent))}",
                f"if [ ! -s {shlex.quote(str(target))} ]; then",
                (
                    f"  aws s3 cp {shlex.quote(obj.s3_uri)} "
                    f"{shlex.quote(str(compressed))} --request-payer requester"
                ),
                (
                    f"  lz4 -dc {shlex.quote(str(compressed))} "
                    f"> {shlex.quote(str(target))}"
                ),
                f"  rm -f {shlex.quote(str(compressed))}",
                "fi",
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o700)
    return path
