"""Future live-trading boundary. No execution code is implemented here yet."""

from hlcopy.trading.permissions import TradingPermissionError, assert_source_trade_allowed

__all__ = ["TradingPermissionError", "assert_source_trade_allowed"]
