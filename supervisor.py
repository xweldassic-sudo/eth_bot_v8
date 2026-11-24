"""Супервизор: запускает бота дочерним процессом и поднимает его снова, если он упал или его перезапустил
сторож (код выхода EXIT_STALL). `python bot.py` запускает именно его; сам бот работает как `bot.py --worker`.

Не перезапускает: штатную остановку (код 0), Ctrl+C / закрытие окна, ошибку настройки (код EXIT_CONFIG)
и бот, который несколько раз подряд падает сразу после старта."""
import signal
import subprocess
import sys
import time

import config as C

RESTART_DELAY_SEC = 5
MIN_UPTIME_SEC = 60  # дольше этого запуск считается удачным
QUICK_FAIL_LIMIT = 3  # столько быстрых падений подряд, и супервизор сдаётся
STOP_GRACE_SEC = 20


def say(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} supervisor: {msg}", file=sys.stderr, flush=True)


def run(cmd, cwd=None, popen=subprocess.Popen, sleep=time.sleep, clock=time.monotonic, install_signals=True):
    stopping = {"flag": False}

    def on_signal(signum, frame):
        stopping["flag"] = True

    if install_signals:
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                try:
                    signal.signal(getattr(signal, name), on_signal)
                except (ValueError, OSError):
                    pass

    quick_fails = 0
    while True:
        started = clock()
        child = popen(cmd, cwd=cwd)
        say(f"бот запущен, PID {child.pid}")
        while child.poll() is None:
            if stopping["flag"]:
                say("получен сигнал остановки, жду завершения бота")
                deadline = clock() + STOP_GRACE_SEC
                while child.poll() is None and clock() < deadline:
                    sleep(0.2)
                if child.poll() is None:
                    child.terminate()
                    sleep(1)
                return 0
            sleep(0.5)
        code = child.returncode
        uptime = clock() - started
        if stopping["flag"] or code == 0:
            say("бот остановлен")
            return 0
        if code == C.EXIT_CONFIG:
            say("ошибка настройки (.env), перезапуск не поможет")
            return code
        if code == C.EXIT_STALL or uptime >= MIN_UPTIME_SEC:
            quick_fails = 0
        else:
            quick_fails += 1
            if quick_fails >= QUICK_FAIL_LIMIT:
                say(f"бот {quick_fails} раза подряд падает сразу после запуска (код {code}), останавливаюсь. См. logs/bot.log")
                return code
        why = "сторож: Telegram не отвечал" if code == C.EXIT_STALL else f"код выхода {code}"
        say(f"бот завершился ({why}), работал {uptime:.0f} с, перезапуск через {RESTART_DELAY_SEC} с")
        sleep(RESTART_DELAY_SEC)
        if stopping["flag"]:
            return 0
