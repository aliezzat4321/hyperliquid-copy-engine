from hlcopy.profitability import margin_snapshot_cli


def test_parser_default_output() -> None:
    args = margin_snapshot_cli.build_parser().parse_args([])
    assert str(args.output) == "data/research/margin_metadata.jsonl"
    assert args.dex == ""
