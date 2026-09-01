#!/usr/bin/env python3
"""Root-only interactive setup for the VM Trello observability credential."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

BOARD_ID = "6a9713c265a75ed50d4181d7"
LIST_IDS = {
    "6a9713db6cfc74eee5b812b1",
    "6a9713e3666e881387b18b9a",
    "6a9713fdfc33292cc90f5486",
    "6a9713f546dc3d1c3907c634",
    "6a97140a2df53d4869073c91",
}
ENV_PATH = Path("/etc/hyperliquid-ai-team/trello.env")


def api_get(
    path: str,
    key: str,
    token: str,
    request: Callable[..., Any] = urllib.request.urlopen,
) -> Any:
    query = urllib.parse.urlencode({"key": key, "token": token})
    req = urllib.request.Request(f"https://api.trello.com/1{path}?{query}")
    with request(req, timeout=15) as response:
        return json.loads(response.read())


def verify(key: str, token: str, request: Callable[..., Any] = urllib.request.urlopen) -> None:
    member = api_get("/members/me", key, token, request)
    if not member.get("id"):
        raise RuntimeError("Trello member verification failed")
    board = api_get(f"/boards/{BOARD_ID}", key, token, request)
    if board.get("id") != BOARD_ID or board.get("closed"):
        raise RuntimeError("exact Trello board is unavailable")
    lists = api_get(f"/boards/{BOARD_ID}/lists", key, token, request)
    visible = {row.get("id") for row in lists if not row.get("closed")}
    if visible != LIST_IDS:
        missing = sorted(LIST_IDS - visible)
        raise RuntimeError(f"exact Trello lists unavailable: {','.join(missing)}")


def store(path: Path, key: str, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"TRELLO_API_KEY={key}\nTRELLO_TOKEN={token}\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ENV_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run as root; credential must be root-owned")
    key = getpass.getpass("Trello API key (hidden): ").strip()
    if not key:
        raise SystemExit("API key is required")
    query = urllib.parse.urlencode(
        {
            "expiration": "never",
            "name": "Hyperliquid AI Team VM",
            "scope": "read,write",
            "response_type": "token",
            "key": key,
        }
    )
    print("Authorize read+write access in your browser:")
    print("https://trello.com/1/authorize?" + query)
    token = getpass.getpass("Trello token (hidden): ").strip()
    if not token:
        raise SystemExit("token is required")
    verify(key, token)
    store(args.output, key, token)
    print("TRELLO_VM_AUTH=READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
