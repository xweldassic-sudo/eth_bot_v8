"""Тесты исправлений: реальный R у тейка, срабатывание и стоп на одной свече, срок жизни ордера (3 свечи),
касания по 15m-свечам, надёжная доставка и очередь, замок состояния, CHAT_ID."""
import asyncio
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402
import config as C  # noqa: E402
import results  # noqa: E402
import storage  # noqa: E402
import strategy as S  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_lock(monkeypatch):
    monkeypatch.setattr(bot, "STATE_LOCK", asyncio.Lock())


def df_of(rows, freq="h", start="2026-01-01"):
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


def trade(tf="1h", d=1, entry=100.0, stop=95.0, tp=110.0, **extra):
    return {"dir": d, "entry": entry, "stop": stop, "tp": tp, "structure": None,
            "tf": tf, "created": "2026-01-01T00:00:00+00:00", **extra}


def pend(d=1, entry=100.0, stop=95.0, tp=110.0, trigger="above", tf="1h", kind="inside", **extra):
    return {"tf": tf, "kind": kind, "dir": d, "entry": entry, "stop": stop, "tp": tp, "structure": None,
            "trigger": trigger, "created": "2026-01-01T00:00:00+00:00", "checked_next": True, **extra}


FLAT = (98, 99, 97, 98)


# ---------- 1. реальный R у тейка ----------

def test_tp_at_zone_reports_real_rr_not_fixed_two():
    df = df_of([(100, 101, 99, 100), (100, 101, 99, 100), (100, 126, 99, 125)])
    st = {"active": [trade(entry=100, stop=95, tp=125)]}  # R:R 1:5
    ev = S.follow_active(df, st, "1h")
    assert [e.kind for e in ev] == ["tp"]
    assert ev[0].r == pytest.approx(5.0)
    assert "+5.0R" in ev[0].text and "+50.00$" in ev[0].text


# ---------- 2. срабатывание ордера и стоп/тейк на той же свече ----------

def test_stop_order_filled_and_stopped_on_same_candle():
    df = df_of([FLAT, (100, 101, 94, 96)])
    st = {"pending": [pend()], "active": []}
    ev = S.follow_pending(df, st, "1h")
    assert [e.kind for e in ev] == ["filled", "stop"]
    assert st["active"] == [] and st["pending"] == []
    assert ev[1].r == -1.0 and ev[1].key


def test_stop_order_filled_and_tp_on_same_candle():
    df = df_of([FLAT, (100, 111, 99, 110)])
    st = {"pending": [pend()], "active": []}
    ev = S.follow_pending(df, st, "1h")
    assert [e.kind for e in ev] == ["filled", "tp"] and st["active"] == []


def test_limit_order_tp_on_fill_candle_is_not_counted():
    """Лимитный вход: порядок хай/лоу в свече неизвестен, тейк на свече входа не засчитываем."""
    df = df_of([FLAT, (101, 112, 99.5, 111)])
    st = {"pending": [pend(trigger="below")], "active": []}
    ev = S.follow_pending(df, st, "1h")
    assert [e.kind for e in ev] == ["filled"] and len(st["active"]) == 1


def test_fill_keeps_order_id():
    df = df_of([FLAT, (100, 101, 99.5, 100.5)])
    st = {"pending": [pend(id=5)], "active": [], "next_id": 6}
    S.follow_pending(df, st, "1h")
    assert st["active"][0]["id"] == 5


# ---------- 5. срок жизни ордера: 3 свечи ----------

def test_pending_expires_after_three_candles():
    rows = [FLAT] * 6
    kept = {"pending": [pend()], "active": []}
    assert S.follow_pending(df_of(rows[:3]), kept, "1h") == [] and len(kept["pending"]) == 1  # 2-я свеча
    st = {"pending": [pend()], "active": []}
    ev = S.follow_pending(df_of(rows[:4]), st, "1h")  # 3-я свеча после сигнальной
    assert [e.kind for e in ev] == ["cancel"] and "срок жизни" in ev[0].text and "3 свечей" in ev[0].text
    assert st["pending"] == []


def test_order_can_still_fill_on_third_candle():
    df = df_of([FLAT, FLAT, FLAT, (99, 101, 98, 100.5)])
    st = {"pending": [pend()], "active": []}
    assert [e.kind for e in S.follow_pending(df, st, "1h")] == ["filled"]


def test_overdue_order_is_expired_without_fill():
    df = df_of([FLAT, FLAT, FLAT, FLAT, (99, 105, 98, 104)])  # 4-я свеча: окно уже закрыто
    st = {"pending": [pend()], "active": []}
    ev = S.follow_pending(df, st, "1h")
    assert [e.kind for e in ev] == ["cancel"] and st["active"] == []


def test_15m_order_expires_after_three_15m_candles():
    df = df_of([FLAT] * 4, freq="15min")
    st = {"pending": [pend(tf="15m")], "active": []}
    ev = S.follow_pending(df, st, "15m")
    assert [e.kind for e in ev] == ["cancel"]


# ---------- 6. касания по 15m-свечам и догон ----------

def fine_of(rows):
    return df_of(rows, freq="15min")


def test_tracker_ignores_candles_before_armed_and_catches_stop_mid_hour():
    rows = [(100, 101, 90, 95)] * 4 + [(100, 101, 99, 100)] * 2 + [(100, 101, 94, 96)] + [(100, 101, 99, 100)]
    #       00:00-00:45: до закрытия сигнальной свечи            01:30 -> стоп
    st = {"active": [trade()], "pending": []}
    ev = S.track_touches(fine_of(rows), st)
    assert [e.kind for e in ev] == ["stop"] and st["active"] == []
    assert "(15m)" in ev[0].text and "1H" in ev[0].text and "01:30" not in ev[0].text.split("\n")[0]
    assert S.track_touches(fine_of(rows), st) == []


def test_tracker_does_not_repeat_and_moves_seen_pointer():
    rows = [(100, 101, 99, 100)] * 8
    st = {"active": [trade()], "pending": []}
    assert S.track_touches(fine_of(rows), st) == []
    first_seen = st["active"][0]["seen"]
    rows += [(100, 101, 99, 100)] * 2
    assert S.track_touches(fine_of(rows), st) == []
    assert st["active"][0]["seen"] > first_seen


def test_tracker_fills_pending_then_hits_tp_with_real_rr():
    rows = [(98, 99, 97, 98)] * 9 + [(99, 101, 98, 100.5)] + [(100.5, 102, 100, 101)] * 2 + [(101, 111, 100, 110)]
    #                              02:15 вход                                          03:00 тейк
    st = {"pending": [pend(id=3)], "active": [], "next_id": 4}
    ev = S.track_touches(fine_of(rows), st)
    assert [e.kind for e in ev] == ["filled", "tp"]
    assert ev[1].r == pytest.approx(2.0) and st["active"] == [] and st["pending"] == []
    assert ev[0].text.startswith("ETHUSDT · 1H")


def test_tracker_does_not_fill_after_ttl_window():
    rows = [(98, 99, 97, 98)] * 17 + [(99, 101, 98, 100.5)]  # касание в 04:15, окно ордера до 04:00
    st = {"pending": [pend()], "active": []}
    assert S.track_touches(fine_of(rows), st) == [] and len(st["pending"]) == 1


def test_tracker_waits_for_ppr_gate():
    rows = [(98, 99, 97, 98)] * 5 + [(99, 101, 98, 100.5)]
    st = {"pending": [pend(checked_next=False, kind="ppr", half=99.0)], "active": []}
    assert S.track_touches(fine_of(rows), st) == [] and len(st["pending"]) == 1


def test_tracker_stop_on_fill_candle_is_counted():
    rows = [(98, 99, 97, 98)] * 4 + [(99, 101, 94, 96)]  # 01:00 касание входа и стопа
    st = {"pending": [pend()], "active": []}
    ev = S.track_touches(fine_of(rows), st)
    assert [e.kind for e in ev] == ["filled", "stop"] and st["active"] == []


# ---------- 3. надёжная доставка и очередь ----------

class FlakyBot:
    def __init__(self, fail):
        self.fail, self.sent, self.photos = fail, [], []

    async def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("telegram недоступен")
        self.sent.append(text)

    async def send_photo(self, chat_id, photo, caption=None):
        if self.fail:
            raise RuntimeError("telegram недоступен")
        self.photos.append((photo.read(), caption))


class Ctx:
    def __init__(self, fail=False):
        self.bot = FlakyBot(fail)


def use_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(storage, "ZONES_FILE", tmp_path / "zones.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(results, "PATH", tmp_path / "results.jsonl")
    monkeypatch.setattr(C, "SEND_RETRY_DELAYS", (0, 0))


def test_failed_send_goes_to_queue_and_is_resent(tmp_path, monkeypatch):
    use_tmp(tmp_path, monkeypatch)
    ev = S.Event("stop", "1h", "ETHUSDT · 1H · СТОП-ЛОСС\nтест", chart=False)
    down = Ctx(fail=True)
    assert asyncio.run(bot.deliver(down, ev)) is False
    q = storage.load_state()["queue"]
    assert len(q) == 1 and q[0]["why"] == "retry"
    asyncio.run(bot.flush_queue(down))  # всё ещё недоступен: запись остаётся
    assert len(storage.load_state()["queue"]) == 1 and storage.load_state()["queue"][0]["attempts"] == 1
    up = Ctx(fail=False)
    asyncio.run(bot.flush_queue(up))
    assert up.bot.sent == ["ETHUSDT · 1H · СТОП-ЛОСС\nтест"]  # без «Ночные уведомления»
    assert storage.load_state()["queue"] == []


def test_queue_keeps_candle_mark_as_json(tmp_path, monkeypatch):
    use_tmp(tmp_path, monkeypatch)
    t = pd.Timestamp("2026-01-01 23:00", tz="UTC")
    ev = S.Event("fakey", "1h", "ETHUSDT · 1H · ФЕЙК\nтест", {"entry": 100.0}, (t, t), chart=True)

    async def no_chart(ev):
        return None

    monkeypatch.setattr(bot, "_render_chart", no_chart)
    assert asyncio.run(bot.deliver(Ctx(fail=True), ev)) is False  # раньше падало: Timestamp не сериализуется
    item = storage.load_state()["queue"][0]
    assert item["why"] == "retry" and item["mark"] == [t.isoformat(), t.isoformat()]
    ctx = Ctx()
    asyncio.run(bot.flush_queue(ctx))
    assert ctx.bot.sent == ["ETHUSDT · 1H · ФЕЙК\nтест"]
    assert storage.load_state()["queue"] == []


def test_long_text_with_chart_is_not_truncated(tmp_path, monkeypatch):
    use_tmp(tmp_path, monkeypatch)
    png = tmp_path / "x.png"
    png.write_bytes(b"png")

    async def fake_chart(ev):
        return png

    monkeypatch.setattr(bot, "_render_chart", fake_chart)
    text = "ЗАГОЛОВОК\n" + "строка " * 300
    ctx = Ctx()
    assert asyncio.run(bot._send(ctx, S.Event("entry", "1h", text))) is True
    assert ctx.bot.photos == [(b"png", "ЗАГОЛОВОК")] and ctx.bot.sent == [text]
    assert not png.exists()  # временный файл удалён


# ---------- 4. замок состояния ----------

def test_command_during_check_is_not_lost(tmp_path, monkeypatch):
    use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(C, "CHAT_ID", 1)
    df = df_of([FLAT] * 5, freq="15min", start="2026-01-01")
    monkeypatch.setattr(bot.bingx, "closed_candles", lambda tf: df)
    storage.save_state({"last": {"15m": df.index[-2].isoformat()}, "pending": [], "active": [], "queue": []})

    def slow(*a, **k):
        time.sleep(0.4)
        return []

    monkeypatch.setattr(bot, "_process", slow)
    replies = []

    class Msg:
        async def reply_text(self, t, **k):
            replies.append(t)

    class Upd:
        message = Msg()
        effective_chat = type("c", (), {"id": 1})()

    class Args:
        args = ["L", "100", "95", "110", "15m"]

    async def scenario():
        async def late_command():
            await asyncio.sleep(0.1)
            await bot.cmd_addtrade(Upd(), Args())
        await asyncio.gather(bot.check_timeframe(Ctx(), "15m"), late_command())

    asyncio.run(scenario())
    st = storage.load_state()
    assert len(st["active"]) == 1 and st["active"][0]["armed"] and "Сделка добавлена" in replies[0]


# ---------- 6. догон пропущенных свечей ----------

def test_missed_1h_candle_still_deletes_broken_zone(tmp_path, monkeypatch):
    use_tmp(tmp_path, monkeypatch)
    rows = [(100, 100.5, 99.5, 100)] * 3 + [(100, 103.5, 99.8, 103)] + [(101, 101.5, 99.5, 100)] * 2
    df = df_of(rows)
    monkeypatch.setattr(bot.bingx, "closed_candles", lambda tf: df)
    storage.save_zones({"next_id": 2, "zones": [{"id": 1, "type": "R", "low": 101, "high": 102}]})
    storage.save_state({"last": {"1h": df.index[2].isoformat()}, "pending": [], "active": [], "queue": []})
    ctx = Ctx()
    asyncio.run(bot.check_timeframe(ctx, "1h"))
    assert storage.load_zones()["zones"] == []
    assert sum("ЗОНА УДАЛЕНА" in t for t in ctx.bot.sent) == 1
    assert storage.load_state()["last"]["1h"] == df.index[-1].isoformat()


def test_after_downtime_replays_missed_15m_candles_for_trades(tmp_path, monkeypatch):
    """Бот был выключен: первая же проверка 15m догоняет пропущенные касания стопа."""
    use_tmp(tmp_path, monkeypatch)
    rows = [(100, 101, 99, 100)] * 10 + [(100, 101, 94, 96)] + [(100, 101, 99, 100)] * 5
    df = df_of(rows, freq="15min")
    monkeypatch.setattr(bot.bingx, "closed_candles", lambda tf: df)
    t0 = df.index[0].isoformat()
    storage.save_state({"last": {"15m": df.index[-2].isoformat()}, "queue": [], "pending": [],
                        "active": [trade(tf="1h", created=t0, armed=t0)]})
    ctx = Ctx()
    asyncio.run(bot.check_timeframe(ctx, "15m"))
    assert any("СТОП-ЛОСС" in t for t in ctx.bot.sent) and storage.load_state()["active"] == []
    assert len(results._read()) == 1 and results._read()[0]["r"] == -1.0


# ---------- 7. CHAT_ID ----------

def make_update(chat_id, with_message=True):
    return type("U", (), {"message": object() if with_message else None,
                          "effective_chat": type("c", (), {"id": chat_id})()})()


def test_commands_are_closed_when_chat_id_is_not_set(monkeypatch):
    monkeypatch.setattr(C, "CHAT_ID", 0)
    assert bot.allowed(make_update(123)) is False


def test_only_owner_chat_is_allowed(monkeypatch):
    monkeypatch.setattr(C, "CHAT_ID", 5)
    assert bot.allowed(make_update(5)) is True
    assert bot.allowed(make_update(6)) is False
    assert bot.allowed(make_update(5, with_message=False)) is False  # правка сообщения


def test_start_in_setup_mode_only_shows_id(monkeypatch):
    monkeypatch.setattr(C, "CHAT_ID", 0)
    out = []

    class Msg:
        async def reply_text(self, t, **k):
            out.append(t)

    upd = make_update(777)
    upd.message = Msg()
    asyncio.run(bot.cmd_start(upd, None))
    assert "ID этого чата: 777" in out[0] and "/zone" not in out[0]


def test_chat_id_env_parsing(monkeypatch):
    monkeypatch.setenv("CHAT_ID", ' "-1001234" ')
    assert C._chat_id() == -1001234
    monkeypatch.setenv("CHAT_ID", "")
    assert C._chat_id() == 0
    monkeypatch.setenv("CHAT_ID", "abc")
    with pytest.raises(SystemExit):
        C._chat_id()


# ---------- мелкое ----------

def test_one_timeframe_failure_does_not_block_the_other(monkeypatch):
    calls = []

    async def fake_check(ctx, tf):
        calls.append(tf)
        if tf == "15m":
            raise RuntimeError("BingX недоступен")

    async def noop(ctx):
        pass

    monkeypatch.setattr(bot, "check_timeframe", fake_check)
    monkeypatch.setattr(bot, "flush_queue", noop)
    monkeypatch.setattr(bot, "announce_restart", noop)
    asyncio.run(bot.loop(None))
    assert calls == ["15m", "1h"]


def test_results_report_without_chart_makes_no_file(tmp_path, monkeypatch):
    use_tmp(tmp_path, monkeypatch)
    results.add_manual(1, 100.0, 95.0, 110.0, "tp", pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(), "1h")
    text, png = results.report(with_chart=False)
    assert png is None and "Итог: +2.00R" in text
    assert list(tmp_path.glob("results_*.png")) == []
