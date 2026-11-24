import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as C  # noqa: E402
import patterns as P  # noqa: E402
import strategy as S  # noqa: E402


def df_of(rows, freq="15min"):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


def inside_pending(df, entry, stop):
    return {"tf": "15m", "kind": "inside", "dir": 1, "entry": entry, "stop": stop, "tp": entry + 10,
            "structure": None, "trigger": "above", "created": df.index[-2].isoformat(),
            "armed": df.index[-1].isoformat(), "checked_next": True, "id": 1}


FAKEY_SELL_ON_FILL = [(100, 106, 94, 104), (103, 105, 97, 99), (99, 107, 98, 100)]


def test_limit_touch_on_opposite_fakey_candle_is_cancelled():
    df = df_of(FAKEY_SELL_ON_FILL)
    st = {"pending": [inside_pending(df, entry=105.0, stop=95.0)], "active": []}
    ev = S.follow_pending(df, st, "15m")
    assert [e.kind for e in ev] == ["cancel"]
    assert "фейк против направления" in ev[0].text
    assert st["active"] == [] and st["pending"] == []


def test_limit_touch_without_fakey_still_fills():
    rows = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 106, 99, 105.5)]
    df = df_of(rows)
    st = {"pending": [inside_pending(df, entry=105.0, stop=95.0)], "active": []}
    ev = S.follow_pending(df, st, "15m")
    assert [e.kind for e in ev] == ["filled"] and len(st["active"]) == 1


def test_inside_15m_stop_uses_small_buffer(monkeypatch):
    rows = [(100, 101, 99, 100)] * 30
    rows[10] = (100, 110, 100, 109)
    rows[5] = (100, 101, 99, 100)
    rows[11:] = [(106, 109, 105, 107)] * 19
    df = df_of(rows)
    p = P.Pattern("inside", 0, 2, "stop", "тест", {"buy_entry": 107.0, "buy_stop": 104.0,
                                                   "sell_entry": 103.0, "sell_stop": 111.0})
    monkeypatch.setattr(S, "swing_points", lambda df_, n: ([(10, 110.0)], [(5, 100.0)]))
    monkeypatch.setattr(S, "trend_of", lambda h, l: 1)
    monkeypatch.setattr(S, "detect", lambda df_, i: [p])
    monkeypatch.setattr(S, "select", lambda pats, d: (p, None))
    st = {"pending": [], "active": []}
    S.process_15m(df, st)
    assert len(st["pending"]) == 1
    expected = round(104.0 * (1 - C.STOP_BUFFER_1H), 6)
    assert round(st["pending"][0]["stop"], 6) == expected
