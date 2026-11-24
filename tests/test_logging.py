import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402
import config as C  # noqa: E402


def test_main_writes_log_file(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(C, "TELEGRAM_TOKEN", "")
    saved = logging.getLogger().handlers[:]
    try:
        with pytest.raises(SystemExit):
            bot.main()
        for h in logging.getLogger().handlers:
            h.flush()
        text = (tmp_path / "logs" / "bot.log").read_text(encoding="utf-8")
    finally:
        for h in logging.getLogger().handlers:
            h.close()
        logging.getLogger().handlers[:] = saved
        sys.excepthook = sys.__excepthook__
    assert "запуск бота" in text
    assert "TELEGRAM_TOKEN не заполнен" in text
