from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

STAGES = {"research", "validation", "approved", "rejected"}
SOURCE_TYPES = {"hyperliquid_wallet", "external"}
MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP = 10
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _clean_coins(coins: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(coin).strip().upper() for coin in coins if str(coin).strip()))


@dataclass(frozen=True, slots=True)
class WalletSpec:
    id: str
    label: str
    source_type: str
    source_ref: str
    stage: str = "research"
    enabled: bool = True
    coins: tuple[str, ...] = ()
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.id or any(char.isspace() for char in self.id):
            raise ValueError("wallet id must be a non-empty slug without whitespace")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {sorted(SOURCE_TYPES)}")
        if self.stage not in STAGES:
            raise ValueError(f"stage must be one of {sorted(STAGES)}")
        if self.source_type == "hyperliquid_wallet" and not _ADDRESS_RE.fullmatch(self.source_ref):
            raise ValueError("Hyperliquid wallet source_ref must be a 42-character 0x address")
        if not self.source_ref:
            raise ValueError("source_ref is required")
        object.__setattr__(self, "coins", _clean_coins(self.coins))
        if (
            self.source_type == "hyperliquid_wallet"
            and self.stage in {"validation", "approved"}
            and not self.coins
        ):
            raise ValueError(
                "validation/approved Hyperliquid wallets require explicit market coins"
            )

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        row["coins"] = list(self.coins)
        return row

    @classmethod
    def from_dict(cls, row: dict[str, object]) -> WalletSpec:
        return cls(
            id=str(row["id"]),
            label=str(row.get("label", row["id"])),
            source_type=str(row["source_type"]),
            source_ref=str(row["source_ref"]),
            stage=str(row.get("stage", "research")),
            enabled=bool(row.get("enabled", True)),
            coins=tuple(str(value) for value in row.get("coins", [])),
            notes=str(row.get("notes", "")),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
        )


class WalletRegistry:
    """Atomic source registry separating research, validation, and trading approval."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def init(self) -> None:
        if not self.path.exists():
            self._save(())

    def load(self) -> tuple[WalletSpec, ...]:
        if not self.path.exists():
            return ()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            raise ValueError("unsupported wallet registry version")
        wallets = tuple(WalletSpec.from_dict(row) for row in payload.get("wallets", []))
        self._assert_valid(wallets)
        return wallets

    @staticmethod
    def _assert_valid(wallets: tuple[WalletSpec, ...] | list[WalletSpec]) -> None:
        ids = [wallet.id for wallet in wallets]
        if len(ids) != len(set(ids)):
            raise ValueError("wallet registry contains duplicate ids")
        addresses = [
            wallet.source_ref.lower()
            for wallet in wallets
            if wallet.source_type == "hyperliquid_wallet"
        ]
        if len(addresses) != len(set(addresses)):
            raise ValueError("wallet registry contains duplicate Hyperliquid addresses")
        active_users = [
            wallet
            for wallet in wallets
            if wallet.enabled
            and wallet.source_type == "hyperliquid_wallet"
            and wallet.stage in {"validation", "approved"}
        ]
        if len(active_users) > MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP:
            raise ValueError(
                "active Hyperliquid validation/approved wallets exceed the per-IP "
                f"limit of {MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP}; use another validation shard"
            )

    def add(self, wallet: WalletSpec) -> WalletSpec:
        wallets = list(self.load())
        if any(existing.id == wallet.id for existing in wallets):
            raise ValueError(f"wallet id already exists: {wallet.id}")
        if wallet.source_type == "hyperliquid_wallet" and any(
            existing.source_type == "hyperliquid_wallet"
            and existing.source_ref.lower() == wallet.source_ref.lower()
            for existing in wallets
        ):
            raise ValueError(f"Hyperliquid wallet already exists: {wallet.source_ref.lower()}")
        now = _now()
        stored = replace(
            wallet,
            created_at=wallet.created_at or now,
            updated_at=now,
        )
        wallets.append(stored)
        self._assert_valid(wallets)
        self._save(wallets)
        return stored

    def update(
        self,
        wallet_id: str,
        *,
        stage: str | None = None,
        enabled: bool | None = None,
        coins: tuple[str, ...] | None = None,
        notes: str | None = None,
    ) -> WalletSpec:
        wallets = list(self.load())
        for index, wallet in enumerate(wallets):
            if wallet.id != wallet_id:
                continue
            updated = replace(
                wallet,
                stage=stage if stage is not None else wallet.stage,
                enabled=enabled if enabled is not None else wallet.enabled,
                coins=_clean_coins(coins) if coins is not None else wallet.coins,
                notes=notes if notes is not None else wallet.notes,
                updated_at=_now(),
            )
            wallets[index] = updated
            self._assert_valid(wallets)
            self._save(wallets)
            return updated
        raise KeyError(wallet_id)

    def remove(self, wallet_id: str) -> None:
        wallets = list(self.load())
        filtered = [wallet for wallet in wallets if wallet.id != wallet_id]
        if len(filtered) == len(wallets):
            raise KeyError(wallet_id)
        self._save(filtered)

    def active_hyperliquid_wallets(
        self,
        *,
        stages: frozenset[str] = frozenset({"validation", "approved"}),
    ) -> tuple[WalletSpec, ...]:
        return tuple(
            wallet
            for wallet in self.load()
            if wallet.enabled
            and wallet.stage in stages
            and wallet.source_type == "hyperliquid_wallet"
        )

    def market_coins(
        self,
        *,
        stages: frozenset[str] = frozenset({"validation", "approved"}),
    ) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                coin
                for wallet in self.load()
                if wallet.enabled and wallet.stage in stages
                for coin in wallet.coins
            )
        )

    def _save(self, wallets: tuple[WalletSpec, ...] | list[WalletSpec]) -> None:
        self._assert_valid(wallets)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": _now(),
            "wallets": [wallet.to_dict() for wallet in wallets],
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.path.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
