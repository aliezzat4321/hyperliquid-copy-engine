from hlcopy.resolver.identify_cli import build_parser


def test_production_cli_uses_measured_bounded_finalist_cap() -> None:
    args = build_parser().parse_args(["evidence.csv"])
    assert args.max_candidates_to_verify == 64
    assert args.historical_entry_time_tolerance_ms == 300_000
