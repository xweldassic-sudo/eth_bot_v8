import asyncio
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402
import storage  # noqa: E402
import strategy as S  # noqa: E402


def df_of(rows):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


def trade(tf="1h", d=1, entry=100.0, stop=95.0, tp=110.0, **extra):
    return {"dir": d, "entry": entry, "stop": stop, "tp": tp, "structure": None,
            "tf": tf, "created": "2026-01-01T00:00:00+00:00", **extra}


def test_long_stop_notifies_minus_one_r():
    df = df_of([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 94, 96)])
    st = {"active": [trade()]}
    ev = S.follow_active(df, st, "1h")
    assert [e.kind for e in ev] == ["stop"]
    assert "-1.0R" in ev[0].text and "-10.00$" in ev[0].text
    assert st["active"] == []


def test_long_tp_notifies_plus_rr():
    df = df_of([(100, 101, 99, 100), (100, 101, 99, 100), (100, 111, 99, 110.5)])
    st = {"active": [trade()]}
    ev = S.follow_active(df, st, "1h")
    assert [e.kind for e in ev] == ["tp"]
    assert "+2.0R" in ev[0].text and "+20.00$" in ev[0].text


def test_short_stop_and_tp_directions():
    df = df_of([(100, 101, 99, 100), (100, 101, 99, 100), (100, 106, 99, 105)])
    ev = S.follow_active(df, {"active": [trade(d=-1, entry=100, stop=105, tp=90)]}, "1h")
    assert [e.kind for e in ev] == ["stop"]
    df = df_of([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 89, 91)])
    ev = S.follow_active(df, {"active": [trade(d=-1, entry=100, stop=105, tp=90)]}, "1h")
    assert [e.kind for e in ev] == ["tp"]


def test_no_event_while_inside_levels():
    df = df_of([(100, 101, 99, 100), (100, 101, 99, 100), (100, 102, 98, 101)])
    st = {"active": [trade()]}
    assert S.follow_active(df, st, "1h") == []
    assert len(st["active"]) == 1


def test_trade_is_not_processed_on_other_timeframe():
    df = df_of([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 94, 96)])
    st = {"active": [trade(tf="15m")]}
    assert S.follow_active(df, st, "1h") == []
    assert len(st["active"]) == 1


def test_legacy_trade_without_tf_is_15m():
    df = df_of([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 94, 96)])
    legacy = trade()
    legacy.pop("tf")
    ev = S.follow_active(df, {"active": [legacy]}, "15m")
    assert [e.kind for e in ev] == ["stop"]


def test_1h_trade_has_no_reversal_exit():
    rows = [(100, 101, 99, 100)] * 10 + [(100, 101, 99, 100.5)]
    rows += [(100, 100.5, 96, 96.5), (96.5, 100, 96.2, 99.6)]
    df = df_of(rows)
    st = {"active": [trade(tf="1h", entry=100, stop=94, tp=130)]}
    assert all(e.kind != "exit" for e in S.follow_active(df, st, "1h"))


def _pin_setup():
    rows = [(110 - k, 111 - k, 109 - k, 109.5 - k) for k in range(10)]
    rows.append((100.2, 100.6, 96.5, 100.4))
    zones = [{"id": 1, "type": "S", "low": 97, "high": 99}, {"id": 2, "type": "R", "low": 120, "high": 121}]
    return df_of(rows), zones


def test_same_direction_second_trade_on_same_tf_is_skipped():
    d, zones = _pin_setup()
    ev = S.process_1h(d, zones, {"pending": [], "active": [trade(tf="1h", d=1)]})
    assert [e.kind for e in ev] == ["skip"] and "уже есть открытая сделка" in ev[0].text


def test_opposite_direction_trade_does_not_block():
    d, zones = _pin_setup()
    ev = S.process_1h(d, zones, {"pending": [], "active": [trade(tf="15m", d=-1, entry=100, stop=105, tp=90)]})
    assert [e.kind for e in ev] == ["entry"]


def test_pending_limit_fill_is_announced():
    df = df_of([(100, 101, 99, 100), (100, 101, 99.5, 100.5), (100.5, 101, 99.5, 100.5)])
    pend = {"tf": "1h", "kind": "pin", "dir": 1, "entry": 100.0, "stop": 95.0, "tp": 110.0,
            "structure": None, "trigger": "below", "created": df.index[0].isoformat(), "checked_next": True}
    st = {"pending": [pend], "active": []}
    ev = S.follow_pending(df, st, "1h")
    assert [e.kind for e in ev] == ["filled"] and len(st["active"]) == 1 and st["pending"] == []
    assert "ОРДЕР СРАБОТАЛ" in ev[0].text


def test_same_direction_on_other_tf_does_not_block():
    d, zones = _pin_setup()
    ev = S.process_1h(d, zones, {"pending": [], "active": [trade(tf="15m", d=1)]})
    assert [e.kind for e in ev] == ["entry"]


def test_legacy_trade_without_tf_counts_as_15m():
    d, zones = _pin_setup()
    legacy = trade(tf="1h", d=1)
    legacy.pop("tf")
    ev = S.process_1h(d, zones, {"pending": [], "active": [legacy]})
    assert [e.kind for e in ev] == ["entry"]


def test_events_are_sent_at_any_time_of_day(tmp_path, monkeypatch):
    """Тихих часов больше нет: стоп и вход приходят и ночью."""
    monkeypatch.setattr(storage, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(storage, "ZONES_FILE", tmp_path / "zones.json")
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text):
            sent.append(text)

    class Ctx:
        bot = FakeBot()

    class NightDT(bot.datetime):
        @classmethod
        def now(cls, tz=None):
            return bot.datetime(2026, 11, 20, 23, 30, tzinfo=bot.MSK)

    monkeypatch.setattr(bot, "datetime", NightDT)
    stop = S.Event("stop", "1h", "ETHUSDT · 1H · СТОП-ЛОСС · ПОКУПКА\nтест", {}, None, chart=False)
    entry = S.Event("entry", "1h", "ETHUSDT · 1H · ВХОД\nтест", {}, None, chart=False)
    asyncio.run(bot.deliver(Ctx(), stop))
    asyncio.run(bot.deliver(Ctx(), entry))
    assert len(sent) == 2 and storage.load_state()["queue"] == []
