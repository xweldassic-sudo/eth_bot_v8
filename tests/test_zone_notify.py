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


def test_zone_deleted_notification_is_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(storage, "ZONES_FILE", tmp_path / "zones.json")
    df = df_of([(100, 101, 99, 100), (100, 103, 99.5, 102.5)])
    zones = [{"id": 1, "type": "R", "low": 101, "high": 102}]
    ev = S.process_1h(df, zones, {"pending": [], "active": []})
    assert [e.kind for e in ev] == ["zone_deleted"] and zones == []
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text):
            sent.append(text)

    class Ctx:
        bot = FakeBot()

    asyncio.run(bot.deliver(Ctx(), ev[0]))
    assert len(sent) == 1 and "ЗОНА УДАЛЕНА" in sent[0] and "сопротивления #1" in sent[0]


def test_zone_kept_when_close_inside_zone(tmp_path, monkeypatch):
    df = df_of([(100, 101, 99, 100), (100, 103, 99.5, 101.5)])
    zones = [{"id": 1, "type": "R", "low": 101, "high": 102}]
    ev = S.process_1h(df, zones, {"pending": [], "active": []})
    assert [e for e in ev if e.kind == "zone_deleted"] == [] and len(zones) == 1
