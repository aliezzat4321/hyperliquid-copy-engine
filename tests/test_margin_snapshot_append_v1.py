from hlcopy.profitability.margin_snapshot_cli import build_parser


def test_margin_snapshot_module_imports() -> None:
    assert build_parser().prog.endswith("margin_snapshot_cli")
