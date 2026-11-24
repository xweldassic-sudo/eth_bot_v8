import asyncio
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402
import storage  # noqa: E402


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append(text)


class Ctx:
    def __init__(self):
        self.bot = FakeBot()


def use_tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(storage, "ZONES_FILE", tmp_path / "zones.json")


def test_parse_trade_long_and_short_defaults():
    t, err = bot.parse_trade(["L", "2400", "2380", "2440"])
    assert err is None and t["dir"] == 1 and t["tf"] == "1h" and t["stop"] == 2380
    t, err = bot.parse_trade(["s", "2400", "2420", "2360", "15m"])
    assert err is None and t["dir"] == -1 and t["tf"] == "15m"


def test_parse_trade_rejects_bad_input():
    assert bot.parse_trade(["L", "2400", "2380"])[1]
    assert bot.parse_trade(["X", "2400", "2380", "2440"])[1]
    assert bot.parse_trade(["L", "2400", "2380", "2440", "4h"])[1]
    assert bot.parse_trade(["L", "a", "2380", "2440"])[1]
    assert bot.parse_trade(["L", "2400", "2410", "2440"])[1]
    assert bot.parse_trade(["S", "2400", "2420", "2440"])[1]


def test_assign_ids_keeps_existing_and_is_unique():
    st = {"active": [{"dir": 1}, {"dir": -1, "id": 7}], "pending": [{"dir": 1}]}
    bot._assign_ids(st)
    ids = [x["id"] for x in st["active"] + st["pending"]]
    assert ids == [1, 7, 2] and st["next_id"] == 3


def test_delete_trade_by_id(tmp_path, monkeypatch):
    use_tmp_storage(tmp_path, monkeypatch)
    storage.save_state({"active": [{"id": 1, "dir": 1, "entry": 1.0, "stop": 0.5, "tp": 2.0}],
                        "pending": [{"id": 2, "dir": -1, "entry": 3.0, "stop": 4.0, "tp": 2.0}],
                        "last": {}, "queue": [], "next_id": 3})
    st = storage.load_state()
    st["active"] = [a for a in st["active"] if a["id"] != 1]
    storage.save_state(st)
    assert [a["id"] for a in storage.load_state()["active"]] == []
    assert len(storage.load_state()["pending"]) == 1
