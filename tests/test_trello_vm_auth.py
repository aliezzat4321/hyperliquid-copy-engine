from __future__ import annotations

import importlib.util
import os
import urllib.parse
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "scripts" / "trello_vm_auth.py"
SPEC = importlib.util.spec_from_file_location("trello_vm_auth", PATH)
assert SPEC and SPEC.loader
auth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auth)


class Response:
    def __init__(self, text: str) -> None:
        self.value = text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self.value


def requester(req, timeout=0):
    assert timeout == 15
    query = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
    assert query == {"key": ["api-key"], "token": ["api-token"]}
    if "/members/me" in req.full_url:
        return Response('{"id":"member"}')
    if req.full_url.split("?")[0].endswith("/lists"):
        rows = ",".join(
            f'{{"id":"{item}","closed":false}}' for item in auth.LIST_IDS
        )
        return Response("[" + rows + "]")
    return Response(f'{{"id":"{auth.BOARD_ID}","closed":false}}')


def test_verify_checks_member_exact_board_and_lists() -> None:
    auth.verify("api-key", "api-token", requester)


def test_store_is_mode_0600_and_contains_both_values(tmp_path: Path) -> None:
    path = tmp_path / "trello.env"
    auth.store(path, "api-key", "api-token")
    assert path.read_text() == "TRELLO_API_KEY=api-key\nTRELLO_TOKEN=api-token\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_main_uses_hidden_inputs_prints_url_and_ready_after_verify(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    answers = iter(["api-key", "api-token"])
    monkeypatch.setattr(auth.os, "geteuid", lambda: 0)
    monkeypatch.setattr(auth.getpass, "getpass", lambda _prompt: next(answers))
    verified = []
    monkeypatch.setattr(auth, "verify", lambda key, token: verified.append((key, token)))
    monkeypatch.setattr(auth.sys, "argv", ["trello_vm_auth.py", "--output", str(tmp_path / "env")])
    assert auth.main() == 0
    output = capsys.readouterr().out
    assert "api-token" not in output
    assert "api-key" in output  # authorization URL necessarily contains the public API key
    assert "scope=read%2Cwrite" in output
    assert output.rstrip().endswith("TRELLO_VM_AUTH=READY")
    assert verified == [("api-key", "api-token")]
    assert os.stat(tmp_path / "env").st_mode & 0o777 == 0o600
