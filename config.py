import os
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")


# Коды выхода: супервизор (supervisor.py) по ним решает, перезапускать бота или нет
EXIT_STALL = 75  # сторож: Telegram-полинг не отвечал, нужен перезапуск
EXIT_CONFIG = 78  # ошибка настройки (.env): перезапуск не поможет


def _chat_id():
    raw = os.getenv("CHAT_ID", "0").strip().strip("\"'")
    try:
        return int(raw or 0)
    except ValueError:
        print("CHAT_ID в .env должен быть числом, например 123456789 или -1001234567890", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)


# 0 = не задан: бот работает в режиме настройки и отвечает только на /start и /id
CHAT_ID = _chat_id()

SYMBOL = "ETH-USDT"
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

RISK_USD = 10.0
RR = 2.0

STOP_BUFFER_1H = 0.001
STOP_BUFFER_15M = 0.005

SWING_N = 5
SWING_N_15M = 3

# Подтверждение разворота на 1H: пользователь ответил по-разному, ждёт решения
CONFIRM_REVERSAL_1H = False

# Откат на 15m: минимальная глубина и максимальное удаление входа от дна отката (доли)
PULLBACK_MIN = 0.382
ENTRY_MAX_RETRACE = 0.5

# Поглощение: тело поглощающей свечи должно занимать не меньше этой доли её диапазона (иначе цвет свечи
# зависит от биржи), и она не должна быть пин-баром в обратную сторону
ENGULF_MIN_BODY = 0.3

# Структура тренда на 15m: откат, который ушёл ниже начала последней волны, ломает тренд.
# Начало волны — минимум (максимум) между двумя последними микро-вершинами. Значимым считается откат
# не меньше LEG_DIP_MULT типичных свечей (медианный диапазон за LEG_LOOKBACK свечей до вершины)
LEG_LOOKBACK = 30
LEG_DIP_MULT = 2.0

REVERSAL_WINDOW = 20
RETEST_TOLERANCE = 0.001

PRESSURE_BARS = 5

# Срок жизни отложенного ордера: сколько свечей его таймфрейма после сигнальной он ждёт срабатывания
PENDING_TTL_CANDLES = 3

# Отправка в Telegram: паузы между повторными попытками (с) и предел попыток для очереди недоставленных
SEND_RETRY_DELAYS = (2, 5, 15)
QUEUE_MAX_ATTEMPTS = 200

# Telegram: связь идёт через прокси, соединения рвутся, поэтому таймауты выставлены с запасом.
# Прокси можно задать явно (TELEGRAM_PROXY=http://host:port); иначе берётся системный/из переменных окружения.
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()
TG_CONNECT_TIMEOUT = 10
TG_READ_TIMEOUT = 20  # ответ на обычный запрос
TG_WRITE_TIMEOUT = 30  # загрузка картинки через медленный прокси
TG_POOL_TIMEOUT = 10
TG_POLL_READ_TIMEOUT = 15  # к ответу на getUpdates прибавляется время длинного опроса

# Сторож полинга: если getUpdates не отвечал успешно столько секунд, бот перезапускается целиком
# (процесс-супервизор поднимет его за несколько секунд, состояние хранится на диске).
POLL_STALL_SEC = 300
WATCHDOG_CHECK_SEC = 15

# Свечи у биржи запрашиваем только после ожидаемого закрытия свечи (а не каждые 30 с)
CANDLE_FETCH_LAG_SEC = 1

# matplotlib/pyplot не потокобезопасны: все построения графиков идут под этим замком
PLOT_LOCK = threading.Lock()

CHECK_INTERVAL_SEC = 30
CANDLES_LIMIT = 300
CHART_BARS = 100
