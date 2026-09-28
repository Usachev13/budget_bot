"""Настройки проекта. На PythonAnywhere значения берутся из переменных окружения."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BUDGET_DB", os.path.join(BASE_DIR, "budget.db"))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
# случайная строка в адресе вебхука, чтобы чужой не мог слать запросы
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
# только этот telegram id может писать боту (0 = проверка выключена)
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", "0") or 0)
# пароль для входа на дашборд
DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "change-me-too")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
GROQ_LLM_MODEL = os.environ.get("GROQ_LLM_MODEL", "llama-3.3-70b-versatile")

# Google Таблица со справочником категорий, правил и счетов
SHEET_ID = os.environ.get("SHEET_ID", "")

BASE_CURRENCY = "RUB"
SALARY_BUDGET = 175000          # месячный лимит расходов из зарплаты, ₽
IP_SPLIT = {                     # распределение денег ИП и процентов
    "Переезд": 0.60,
    "Закрытие разрыва": 0.25,    # после выхода в плюс — в инвестиции
    "Праздники и подарки": 0.15,
}
TAX_RESERVE_SHARE = 0.10         # доля оборота ИП в налоговую копилку
RELOCATION_GOAL_ILS = 20000
RELOCATION_DEADLINE = "2027-04-30"
TICKETS_GOAL_RUB = 80000
