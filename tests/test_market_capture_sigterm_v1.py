from __future__ import annotations

import asyncio
import signal

from hlcopy.market.capture import _run_collector_with_sigterm


class _FakeSignalLoop:
    def __init__(self) -> None:
        self.handlers: dict[int, object] = {}
        self.removed: list[int] = []

    def add_signal_handler(self, sig: int, callback) -> None:
        self.handlers[sig] = callback

    def remove_signal_handler(self, sig: int) -> bool:
        self.removed.append(sig)
        self.handlers.pop(sig, None)
        return True


class _FakeCollector:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def run(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Mirrors HyperliquidMarketCollector.run(): its finally closes/flushed sink.
            self.closed = True


def test_sigterm_cancels_collector_and_reaches_finally() -> None:
    async def scenario() -> None:
        loop = _FakeSignalLoop()
        collector = _FakeCollector()
        runner = asyncio.create_task(
            _run_collector_with_sigterm(collector, signal_loop=loop)
        )
        await collector.started.wait()
        callback = loop.handlers[signal.SIGTERM]
        assert callable(callback)
        callback()
        await runner

        assert collector.closed is True
        assert signal.SIGTERM in loop.removed

    asyncio.run(scenario())


def test_external_cancellation_is_not_swallowed() -> None:
    async def scenario() -> None:
        loop = _FakeSignalLoop()
        collector = _FakeCollector()
        runner = asyncio.create_task(
            _run_collector_with_sigterm(collector, signal_loop=loop)
        )
        await collector.started.wait()
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("external cancellation must propagate")

        assert collector.closed is True
        assert signal.SIGTERM in loop.removed

    asyncio.run(scenario())
