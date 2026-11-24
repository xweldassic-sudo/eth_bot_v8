"""Тесты устойчивости: сторож полинга, супервизор, лог команд, короткие сетевые ошибки,
запрос свечей только после закрытия, отсутствие тихих часов."""
import asyncio
import logging
import signal
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402
import config as C  # noqa: E402
import health  # noqa: E402
import storage  # noqa: E402
import strategy as S  # noqa: E402
import supervisor  # noqa: E402
from telegram.error import BadRequest, NetworkError  # noqa: E402


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "STATE_LOCK", asyncio.Lock())
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(storage, "ZONES_FILE", tmp_path / "zones.json")
    monkeypatch.setattr(C, "SEND_RETRY_DELAYS", (0, 0))
    health.POLL.reset()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---------- PollState и сторож ----------

def test_poll_state_counts_errors_and_reports_recovery():
    clk = Clock()
    p = health.PollState(clock=clk)
    assert p.error() == 1 and p.error() == 2
    clk.t += 40
    assert p.ok() == (2, 40) and p.errors == 0
    assert p.ok() is None


def test_watchdog_does_nothing_while_polling_is_alive():
    clk = Clock()
    p = health.PollState(clock=clk)
    clk.t += C.POLL_STALL_SEC - 1
    codes = []
    assert health.check_stall(p, exit_fn=codes.append) is False and codes == []


def test_watchdog_restarts_process_when_polling_is_silent(tmp_path):
    clk = Clock()
    p = health.PollState(clock=clk)
    p.error()
    clk.t += C.POLL_STALL_SEC + 10
    codes = []
    assert health.check_stall(p, exit_fn=codes.append) is True
    assert codes == [C.EXIT_STALL]
    info = health.read_restart_reason()
    assert info["errors"] == 1 and info["silent_sec"] >= C.POLL_STALL_SEC


def test_successful_get_updates_keeps_watchdog_quiet():
    clk = Clock()
    p = health.PollState(clock=clk)
    clk.t += C.POLL_STALL_SEC - 5
    p.ok()
    clk.t += C.POLL_STALL_SEC - 5
    assert health.check_stall(p, exit_fn=lambda c: pytest.fail("не должен перезапускать")) is False


class FakeBot:
    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    async def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("down")
        self.sent.append(text)


class Ctx:
    def __init__(self, fail=False):
        self.bot = FakeBot(fail)


def test_restart_is_announced_once_and_marker_removed():
    health.restart_mark_path().write_text('{"at": "2026-11-23T02:00:00+00:00", "silent_sec": 330, "errors": 7}')
    ctx = Ctx()
    asyncio.run(bot.announce_restart(ctx))
    asyncio.run(bot.announce_restart(ctx))
    assert len(ctx.bot.sent) == 1
    assert "6 мин" in ctx.bot.sent[0] and "05:00 МСК" in ctx.bot.sent[0]
    assert not health.restart_mark_path().exists()


def test_restart_notice_goes_to_queue_if_telegram_is_down():
    health.restart_mark_path().write_text('{"at": "2026-11-23T02:00:00+00:00", "silent_sec": 300, "errors": 1}')
    asyncio.run(bot.announce_restart(Ctx(fail=True)))
    assert len(storage.load_state()["queue"]) == 1 and not health.restart_mark_path().exists()


# ---------- супервизор ----------

class FakeChild:
    def __init__(self, code, polls=1):
        self.returncode, self._polls, self.pid = code, polls, 4242

    def poll(self):
        if self._polls > 0:
            self._polls -= 1
            return None
        return self.returncode

    def terminate(self):
        self._polls = 0


def run_sup(codes, uptimes=None):
    started = []
    codes = list(codes)
    clk = {"t": 0.0}
    ups = list(uptimes or [1] * len(codes))

    def popen(cmd, cwd=None):
        started.append(cmd)
        return FakeChild(codes.pop(0))

    def sleep(sec):
        clk["t"] += sec

    def clock():
        return clk["t"]

    # длительность запуска задаём через подмену часов внутри poll
    real_popen = popen

    def popen_with_uptime(cmd, cwd=None):
        child = real_popen(cmd, cwd)
        up = ups.pop(0)
        orig = child.poll

        def poll():
            r = orig()
            if r is not None:
                clk["t"] += up
            return r
        child.poll = poll
        return child

    rc = supervisor.run(["x"], popen=popen_with_uptime, sleep=sleep, clock=clock, install_signals=False)
    return rc, len(started)


def test_supervisor_restarts_after_watchdog_exit_then_stops_on_clean_exit():
    assert run_sup([C.EXIT_STALL, 0]) == (0, 2)


def test_supervisor_restarts_after_crash_of_long_running_bot():
    assert run_sup([1, 0], uptimes=[3600, 10]) == (0, 2)


def test_supervisor_gives_up_after_repeated_instant_crashes():
    rc, runs = run_sup([1, 1, 1, 0], uptimes=[2, 2, 2, 2])
    assert rc == 1 and runs == supervisor.QUICK_FAIL_LIMIT


def test_supervisor_does_not_restart_on_config_error():
    assert run_sup([C.EXIT_CONFIG, 0]) == (C.EXIT_CONFIG, 1)


def test_supervisor_stops_on_ctrl_c():
    old = signal.getsignal(signal.SIGINT)
    try:
        child = FakeChild(0, polls=10 ** 6)
        calls = {"n": 0}

        def sleep(sec):
            calls["n"] += 1
            if calls["n"] == 1:
                signal.raise_signal(signal.SIGINT)
            if calls["n"] > 3:
                child._polls = 0

        rc = supervisor.run(["x"], popen=lambda cmd, cwd=None: child, sleep=sleep, install_signals=True)
        assert rc == 0
    finally:
        signal.signal(signal.SIGINT, old)


# ---------- ошибки Telegram и лог команд ----------

def test_network_error_while_polling_is_logged_in_one_line(caplog):
    caplog.set_level(logging.WARNING, logger="eth_bot")
    ctx = type("C", (), {"error": NetworkError("httpx.ConnectError: All connection attempts failed")})()
    asyncio.run(bot.on_error(None, ctx))
    asyncio.run(bot.on_error(None, ctx))
    recs = [r for r in caplog.records if r.name == "eth_bot"]
    assert len(recs) == 2 and all(r.exc_info is None and "\n" not in r.getMessage() for r in recs)
    assert "ошибок подряд: 2" in recs[1].getMessage() and health.POLL.errors == 2


def test_bad_request_and_other_errors_keep_full_traceback(caplog):
    caplog.set_level(logging.WARNING, logger="eth_bot")
    for err in (BadRequest("Message is too long"), ValueError("баг в обработчике")):
        asyncio.run(bot.on_error(object(), type("C", (), {"error": err})()))
    recs = [r for r in caplog.records if r.name == "eth_bot"]
    assert len(recs) == 2 and all(r.levelno == logging.ERROR and r.exc_info for r in recs)
    assert health.POLL.errors == 0


def test_tracked_bot_marks_successful_get_updates(monkeypatch):
    async def fake(self, *a, **k):
        return ["u"]

    monkeypatch.setattr(bot.ExtBot, "get_updates", fake)
    obj = bot.TrackedBot.__new__(bot.TrackedBot)
    health.POLL.error()
    assert asyncio.run(bot.TrackedBot.get_updates(obj)) == ["u"]
    assert health.POLL.errors == 0


def make_update(text, chat_id):
    msg = type("M", (), {"text": text})()
    return type("U", (), {"message": msg, "effective_chat": type("c", (), {"id": chat_id})()})()


def test_every_command_is_logged_with_verdict(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="eth_bot")
    monkeypatch.setattr(C, "CHAT_ID", 5)
    for text, chat in (("/zone S 2200 2250", 5), ("/status", 99), ("просто текст", 5)):
        asyncio.run(bot.log_update(make_update(text, chat), None))
    msgs = [r.getMessage() for r in caplog.records if r.name == "eth_bot"]
    assert msgs == ["команда получена: /zone S 2200 2250 | чат 5 | принята",
                    "команда получена: /status | чат 99 | отклонена: чужой чат"]
    monkeypatch.setattr(C, "CHAT_ID", 0)
    caplog.clear()
    asyncio.run(bot.log_update(make_update("/start", 7), None))
    assert "режим настройки" in caplog.records[0].getMessage()


# ---------- запрос свечей только после закрытия ----------

def test_due_logic_for_both_timeframes():
    last = "2026-11-23T04:00:00+00:00"
    ts = pd.Timestamp
    assert bot._due("15m", None) is True
    assert bot._due("15m", last, now=ts("2026-11-23T04:29:59", tz="UTC")) is False  # свеча 04:15 ещё не закрыта
    assert bot._due("15m", last, now=ts("2026-11-23T04:30:02", tz="UTC")) is True
    assert bot._due("1h", last, now=ts("2026-11-23T05:59:00", tz="UTC")) is False
    assert bot._due("1h", last, now=ts("2026-11-23T06:00:05", tz="UTC")) is True


def test_exchange_is_not_queried_between_candle_closes(monkeypatch):
    now = pd.Timestamp.now(tz="UTC")
    last = (now.floor("15min") - pd.Timedelta(minutes=15)).isoformat()  # последняя обработанная = только что закрытая
    storage.save_state({"last": {"15m": last}, "pending": [], "active": [], "queue": []})

    def boom(tf):
        raise AssertionError("запрос к бирже вне момента закрытия свечи")

    monkeypatch.setattr(bot.bingx, "closed_candles", boom)
    asyncio.run(bot.check_timeframe(Ctx(), "15m"))


def test_exchange_is_queried_when_new_candle_is_due(monkeypatch):
    calls = []
    idx = pd.date_range("2026-01-01", periods=5, freq="15min", tz="UTC")
    df = pd.DataFrame([(100, 101, 99, 100)] * 5, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)
    monkeypatch.setattr(bot.bingx, "closed_candles", lambda tf: calls.append(tf) or df)
    storage.save_state({"last": {"15m": idx[-2].isoformat()}, "pending": [], "active": [], "queue": []})
    asyncio.run(bot.check_timeframe(Ctx(), "15m"))
    assert calls == ["15m"] and storage.load_state()["last"]["15m"] == idx[-1].isoformat()


# ---------- нет тихих часов ----------

def test_quiet_hours_are_gone():
    assert not hasattr(bot, "quiet_now") and not hasattr(bot, "evening_reminder")
    assert not hasattr(C, "QUIET_START_MSK") and not hasattr(C, "QUIET_END_MSK")
    import inspect
    assert "tracking" not in inspect.signature(S.process_1h).parameters
    assert "tracking" not in inspect.signature(S.process_15m).parameters


def test_signals_are_produced_at_night(monkeypatch):
    """Свеча, закрывшаяся в 23:00 МСК, даёт сигнал на вход (раньше он молча отбрасывался)."""
    rows = [(110 - k, 111 - k, 109 - k, 109.5 - k) for k in range(10)] + [(100.2, 100.6, 96.5, 100.4)]
    idx = pd.date_range("2026-01-01 16:00", periods=len(rows), freq="h", tz="UTC")  # последняя свеча 02:00 МСК
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)
    zones = [{"id": 1, "type": "S", "low": 97, "high": 99}, {"id": 2, "type": "R", "low": 120, "high": 121}]
    ev = S.process_1h(df, zones, {"pending": [], "active": []})
    assert [e.kind for e in ev] == ["entry"]


def test_status_shows_always_on_and_poll_health(monkeypatch):
    monkeypatch.setattr(C, "CHAT_ID", 1)
    storage.save_state({"last": {}, "pending": [], "active": [], "queue": []})
    storage.save_zones({"next_id": 1, "zones": []})
    out = []

    class Msg:
        async def reply_text(self, t, **k):
            out.append(t)

    class Upd:
        message = Msg()
        effective_chat = type("c", (), {"id": 1})()

    asyncio.run(bot.cmd_status(Upd(), None))
    assert "Уведомления: круглосуточно" in out[0] and "Приём команд Telegram" in out[0]
    assert "Тихие часы" not in out[0]
