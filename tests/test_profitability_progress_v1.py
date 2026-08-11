from hlcopy.profitability.progress import Progress


def test_progress_emits_first_tick(capsys) -> None:
    progress = Progress("test", every=10)
    progress.tick("wallet=abc")
    out = capsys.readouterr().out
    assert "progress label=test count=1" in out
    assert "wallet=abc" in out
    assert "maxrss_mib=" in out
