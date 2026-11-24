import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402
import results  # noqa: E402

UTC = timezone.utc


def use_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "PATH", tmp_path / "results.jsonl")


def test_parse_tp_converts_msk_to_utc():
    r, err = bot.parse_result(["L", "2400", "2380", "2440", "tp", "19.11.2026", "14:30"])
    assert err is None
    assert r["kind"] == "tp" and r["tf"] == "1h" and r["side"] == 1
    assert r["when"] == datetime(2026, 11, 19, 11, 30, tzinfo=UTC)


def test_parse_exit_with_price_and_tf():
    r, err = bot.parse_result(["S", "2400", "2420", "2360", "exit", "2390", "19.11", "12:00", "15m"])
    assert err is None and r["price"] == 2390.0 and r["tf"] == "15m" and r["side"] == -1


def test_parse_rejects_bad_input():
    assert bot.parse_result(["L", "2400", "2410", "2440", "tp", "19.11", "10:00"])[1]
    assert bot.parse_result(["L", "2400", "2380", "2440", "win", "19.11", "10:00"])[1]
    assert bot.parse_result(["L", "2400", "2380", "2440", "exit", "19.11", "10:00"])[1]
    assert bot.parse_result(["L", "2400", "2380", "2440", "tp", "19.11", "25:00"])[1]
    assert bot.parse_result(["L", "2400", "2380", "2440", "tp", "19.11", "10:00", "4h"])[1]
    future = (datetime.now(bot.MSK) + timedelta(days=2)).strftime("%d.%m.%Y")
    assert "будущем" in bot.parse_result(["L", "2400", "2380", "2440", "tp", future, "10:00"])[1]


def test_add_manual_r_values(tmp_path, monkeypatch):
    use_tmp(tmp_path, monkeypatch)
    when = datetime(2026, 11, 19, 8, tzinfo=UTC)
    assert results.add_manual(1, 100.0, 95.0, 110.0, "tp", when, "1h")
    assert results.add_manual(1, 100.0, 95.0, 110.0, "stop", when + timedelta(hours=1), "1h")
    assert results.add_manual(-1, 100.0, 105.0, 90.0, "exit", when + timedelta(hours=2), "15m", price=95.0)
    rows = results._read()
    assert [r["r"] for r in rows] == [2.0, -1.0, 1.0]


def test_duplicate_manual_not_added_twice(tmp_path, monkeypatch):
    use_tmp(tmp_path, monkeypatch)
    when = datetime(2026, 11, 19, 8, tzinfo=UTC)
    assert results.add_manual(1, 100.0, 95.0, 110.0, "tp", when, "1h") is True
    assert results.add_manual(1, 100.0, 95.0, 110.0, "tp", when, "1h") is False
    assert len(results._read()) == 1


def test_report_orders_by_date_not_insert_order(tmp_path, monkeypatch):
    use_tmp(tmp_path, monkeypatch)
    results.add_manual(1, 100.0, 95.0, 110.0, "tp", datetime(2026, 11, 19, 8, tzinfo=UTC), "1h")
    results.add_manual(1, 100.0, 95.0, 110.0, "stop", datetime(2026, 11, 18, 8, tzinfo=UTC), "1h")
    text, _ = results.report()
    assert text.index("18.11") < text.index("19.11")
    assert "Итог: +1.00R" in text


def test_cmd_addresult_replies_with_total(tmp_path, monkeypatch):
    use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(bot.C, "CHAT_ID", 1)
    out = []

    class Msg:
        async def reply_text(self, t, **k):
            out.append(t)

    class Upd:
        message = Msg()
        effective_chat = type("c", (), {"id": 1})()

    class Ctx:
        args = ["L", "2400", "2380", "2440", "tp", "19.11", "14:30"]

    asyncio.run(bot.cmd_addresult(Upd(), Ctx()))
    assert out[0].startswith("Записано в итоги.") and "Итог: +2.00R" in out[0]
