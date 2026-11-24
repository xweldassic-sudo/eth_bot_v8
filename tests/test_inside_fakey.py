import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import patterns as P  # noqa: E402
import strategy as S  # noqa: E402


def df_of(rows):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


FAKEY_SELL = [(100, 106, 94, 104), (103, 105, 97, 99), (99, 107, 98, 100)]
INSIDE_BUY_AT_SUPPORT = (101, 104, 99, 102)
SUPPORT = [{"id": 2, "type": "S", "low": 95, "high": 101}]


def test_inside_after_opposite_fakey_is_not_a_signal():
    df = df_of(FAKEY_SELL + [INSIDE_BUY_AT_SUPPORT])
    assert any(p.kind == "inside" for p in P.detect(df, 3))
    ev = S.process_1h(df, [dict(z) for z in SUPPORT], {"pending": [], "active": []})
    assert [e.kind for e in ev if e.kind in ("entry", "skip")] == []


def test_inside_without_fakey_still_gives_entry():
    neutral = [(100, 106, 94, 100), (100, 105, 97, 99), (99, 104, 98, 100)]
    df = df_of(neutral + [INSIDE_BUY_AT_SUPPORT])
    assert not any(p.kind == "fakey" for k in range(4) for p in P.detect(df, k))
    ev = S.process_1h(df, [dict(z) for z in SUPPORT], {"pending": [], "active": []})
    assert "entry" in [e.kind for e in ev]


def test_open_inside_trade_closed_by_fakey():
    rows = FAKEY_SELL + [INSIDE_BUY_AT_SUPPORT, (102, 105, 100, 101)]
    df = df_of(rows)
    trade = {"dir": 1, "entry": 102.0, "stop": 95.0, "tp": 115.0, "structure": None, "tf": "1h",
             "kind": "inside", "created": df.index[3].isoformat()}
    st = {"pending": [], "active": [trade]}
    ev = S.process_1h(df, [], st)
    exits = [e for e in ev if e.kind == "exit"]
    assert len(exits) == 1 and "ЗАКРЫТЬ ПО ФЕЙКУ" in exits[0].text
    assert st["active"] == []


def test_open_trade_without_kind_not_closed_by_fakey():
    rows = FAKEY_SELL + [INSIDE_BUY_AT_SUPPORT, (102, 105, 100, 101)]
    df = df_of(rows)
    trade = {"dir": 1, "entry": 102.0, "stop": 95.0, "tp": 115.0, "structure": None, "tf": "1h",
             "created": df.index[3].isoformat()}
    st = {"pending": [], "active": [trade]}
    ev = S.process_1h(df, [], st)
    assert [e for e in ev if e.kind == "exit"] == [] and len(st["active"]) == 1
