#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
from pathlib import Path

KEYS = ("RESEARCH_DATABASE_URL", "DATABASE_URL")
SQL = Path("/root/hyperliquid-copy-engine/scripts/postgres_storage_audit.sql")


def _candidate_urls() -> list[tuple[str, str]]:
    seen: set[str] = set()
    rows: list[tuple[str, str]] = []

    def add(source: str, value: str | None) -> None:
        value = (value or "").strip().strip('"').strip("'")
        if value.startswith(("postgres://", "postgresql://")) and value not in seen:
            seen.add(value)
            rows.append((source, value))

    for key in KEYS:
        add(f"current_env:{key}", os.environ.get(key))

    for env_path in (
        Path("/root/hyperliquid-copy-engine/.env"),
        Path("/etc/hyperliquid-copy-engine/invo.env"),
    ):
        if not env_path.is_file():
            continue
        for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in KEYS:
                add(f"file:{env_path.name}:{key.strip()}", value)

    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        environ = entry / "environ"
        try:
            data = environ.read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for item in data.split(b"\0"):
            for key in KEYS:
                prefix = f"{key}=".encode()
                if item.startswith(prefix):
                    add(f"active_process:{key}", item[len(prefix) :].decode(errors="ignore"))
    return rows


def _works(url: str) -> bool:
    result = subprocess.run(
        ["psql", url, "-X", "-Atqc", "SELECT 1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )
    return result.returncode == 0


def main() -> None:
    candidates = _candidate_urls()
    print(f"POSTGRES_CREDENTIAL_CANDIDATES_DISCOVERED={len(candidates)}")
    for source, url in candidates:
        if not _works(url):
            continue
        print(f"AUDIT_CONNECTION_SOURCE={source}")
        result = subprocess.run(
            ["psql", url, "-X", "-v", "ON_ERROR_STOP=1", "-f", str(SQL)],
            check=False,
        )
        if result.returncode != 0:
            raise SystemExit(result.returncode)
        print("READ_ONLY_NO_MUTATION=YES")
        return
    raise SystemExit("POSTGRES_STORAGE_AUDIT_FAIL=no working credential source discovered")


if __name__ == "__main__":
    main()
