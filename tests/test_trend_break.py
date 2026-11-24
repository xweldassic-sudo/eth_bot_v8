import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swings import trend_break  # noqa: E402


def df_close(close):
    idx = pd.date_range("2026-01-01", periods=1, freq="15min", tz="UTC")
    return pd.DataFrame({"open": [close], "high": [close], "low": [close], "close": [close], "volume": [1.0]}, index=idx)


def test_bullish_higher_low_and_close_above_last_high():
    highs = [(10, 110.0), (20, 112.0)]
    lows = [(12, 100.0), (22, 102.0)]
    assert trend_break(df_close(113.0), highs, lows) == 1


def test_no_bullish_without_close_above_high():
    highs = [(10, 110.0), (20, 112.0)]
    lows = [(12, 100.0), (22, 102.0)]
    assert trend_break(df_close(111.0), highs, lows) == 0


def test_no_bullish_when_low_is_lower():
    highs = [(10, 110.0), (20, 112.0)]
    lows = [(12, 100.0), (22, 98.0)]
    assert trend_break(df_close(113.0), highs, lows) == 0


def test_bearish_lower_high_and_close_below_last_low():
    highs = [(10, 112.0), (20, 110.0)]
    lows = [(12, 100.0), (22, 98.0)]
    assert trend_break(df_close(97.0), highs, lows) == -1


def test_not_enough_swings():
    assert trend_break(df_close(113.0), [(10, 110.0)], [(12, 100.0), (22, 102.0)]) == 0
