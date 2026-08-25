from __future__ import annotations

import asyncio

from hlcopy.resolver import identify_cli


class _Result:
    def to_dict(self) -> dict[str, object]:
        return {"status": "UNRESOLVED"}


def test_cli_wires_historical_entry_time_tolerance(monkeypatch, tmp_path) -> None:
    evidence = tmp_path / "evidence.csv"
    args = identify_cli.build_parser().parse_args(
        [
            str(evidence),
            "--historical-entry-time-tolerance-ms",
            "123456",
        ]
    )
    captured: dict[str, object] = {}

    async def fake_identify_wallet_from_csv(path, *, output_dir, config):
        captured["path"] = path
        captured["output_dir"] = output_dir
        captured["config"] = config
        return _Result()

    monkeypatch.setattr(
        identify_cli,
        "identify_wallet_from_csv",
        fake_identify_wallet_from_csv,
    )
    asyncio.run(identify_cli._run(args))

    config = captured["config"]
    assert config.historical_entry_time_tolerance_ms == 123456
