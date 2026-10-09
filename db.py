"""Работа с базой. SQLite, одна таблица операций плюс справочники."""
import sqlite3, os, re, datetime as dt
from contextlib import contextmanager
import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,                 -- current | savings | card | wallet
    currency TEXT NOT NULL DEFAULT 'RUB',
    purpose TEXT DEFAULT '',            -- налоги / билеты / переезд / свободные
    credit_limit REAL DEFAULT 0,
    opening_balance REAL DEFAULT 0,
    opening_date TEXT DEFAULT '2026-01-01',
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY,
    wallet TEXT NOT NULL,               -- salary | ip | income | transfer
    kind TEXT NOT NULL,                 -- expense | income | transfer
    monthly_limit REAL DEFAULT 0,
    sort INTEGER DEFAULT 100
);

-- лимит на конкретный месяц, если отличается от обычного
CREATE TABLE IF NOT EXISTS limit_overrides (
    month TEXT NOT NULL,                -- '2026-10'
    category TEXT NOT NULL,
    monthly_limit REAL NOT NULL,
    PRIMARY KEY (month, category)
);

CREATE TABLE IF NOT EXISTS txns (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,                 -- YYYY-MM-DD
    type TEXT NOT NULL,                 -- expense | income | transfer
    amount REAL NOT NULL,               -- в валюте операции, всегда положительное
    currency TEXT NOT NULL DEFAULT 'RUB',
    amount_rub REAL NOT NULL,
    category TEXT DEFAULT '',
    account_id INTEGER,                 -- откуда (расход, перевод) / куда (доход)
    to_account_id INTEGER,              -- для переводов
    note TEXT DEFAULT '',
    source TEXT DEFAULT 'bot',          -- bot | statement | sheet | manual
    ext_id TEXT UNIQUE,                 -- защита от повторной загрузки выписки
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_txns_date ON txns(date);
CREATE INDEX IF NOT EXISTS idx_txns_cat ON txns(category);

-- остатки по счетам на дату (из выписок или введённые вручную)
CREATE TABLE IF NOT EXISTS balances (
    account_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    balance REAL NOT NULL,              -- для карт: задолженность со знаком минус
    PRIMARY KEY (account_id, date)
);

CREATE TABLE IF NOT EXISTS rates (
    code TEXT PRIMARY KEY,
    rub REAL NOT NULL,
    updated TEXT
);

-- правила автокатегоризации: кусок описания -> категория
CREATE TABLE IF NOT EXISTS rules (
    pattern TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    hits INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- незавершённый диалог бота (ожидание уточнения категории)
CREATE TABLE IF NOT EXISTS pending (
    chat_id INTEGER PRIMARY KEY,
    payload TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

@contextmanager
def conn():
    c = sqlite3.connect(config.DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()

MIGRATIONS = [
    ("accounts", "aliases", "ALTER TABLE accounts ADD COLUMN aliases TEXT DEFAULT ''"),
]


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        for table, column, sql in MIGRATIONS:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                c.execute(sql)
        # раньше в pending была одна строка на чат, из-за этого старые кнопки
        # записывали категорию в новую операцию; теперь у каждого вопроса свой id
        cols = {r["name"] for r in c.execute("PRAGMA table_info(pending)")}
        if "id" not in cols:
            c.execute("DROP TABLE IF EXISTS pending")
            c.execute("""CREATE TABLE pending (
                id INTEGER PRIMARY KEY, chat_id INTEGER, payload TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    seed()


def get_setting(key, default=None):
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    with conn() as c:
        c.execute("""INSERT INTO settings(key,value) VALUES(?,?)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, str(value)))

# ---------------------------------------------------------------- справочники
CATEGORIES = [
    # (название, кошелёк, тип, лимит, сортировка)
    ("Дети и алименты",            "salary", "expense", 70000, 10),
    ("Продукты",                   "salary", "expense", 30000, 20),
    ("Жильё, ЖКХ, связь",          "salary", "expense", 17000, 30),
    ("Кафе и доставка",            "salary", "expense", 15000, 40),
    ("Здоровье и спорт",           "salary", "expense", 12000, 50),
    ("Транспорт",                  "salary", "expense",  8000, 60),
    ("Одежда и покупки",           "salary", "expense",  7000, 70),
    ("Развлечения",                "salary", "expense",  5000, 80),
    ("Красота",                    "salary", "expense",  4000, 90),
    ("Непредвиденное",             "salary", "expense",  7000, 100),

    ("Налоги и взносы ИП",         "ip", "expense", 0, 110),
    ("Переезд",                    "ip", "expense", 0, 120),
    ("Праздники и подарки",        "ip", "expense", 0, 130),
    ("Инвестиции",                 "ip", "expense", 0, 140),

    ("Зарплата",                   "income", "income", 0, 200),
    ("Комиссии ИП",                "income", "income", 0, 210),
    ("Проценты по счетам",         "income", "income", 0, 220),
    ("Прочие доходы",              "income", "income", 0, 230),

    ("Погашение карты",            "transfer", "transfer", 0, 300),
    ("Покупка валюты",             "transfer", "transfer", 0, 310),
    ("Пополнение копилки",         "transfer", "transfer", 0, 320),
    ("Перевод между счетами",      "transfer", "transfer", 0, 330),
]

# слово в сообщении или описании из выписки -> категория
RULES = {
    # продукты
    "пятёроч": "Продукты", "пятероч": "Продукты", "dixy": "Продукты", "дикси": "Продукты",
    "vkusvill": "Продукты", "вкусвилл": "Продукты", "vv_kkm": "Продукты", "магнит": "Продукты",
    "перекрёст": "Продукты", "ашан": "Продукты", "лента": "Продукты", "продукт": "Продукты",
    # кафе
    "кафе": "Кафе и доставка", "ресторан": "Кафе и доставка", "кофе": "Кафе и доставка",
    "papajohns": "Кафе и доставка", "rostics": "Кафе и доставка", "sushi": "Кафе и доставка",
    "kofe": "Кафе и доставка", "додо": "Кафе и доставка", "самокат": "Кафе и доставка",
    "яндекс еда": "Кафе и доставка", "delivery": "Кафе и доставка", "бар": "Кафе и доставка",
    # транспорт
    "такси": "Транспорт", "taxi": "Транспорт", "troyka": "Транспорт", "тройка": "Транспорт",
    "метро": "Транспорт", "бензин": "Транспорт", "азс": "Транспорт", "юрент": "Транспорт",
    "каршеринг": "Транспорт", "самокат аренда": "Транспорт",
    # жильё
    "еирц": "Жильё, ЖКХ, связь", "жкх": "Жильё, ЖКХ, связь", "коммунал": "Жильё, ЖКХ, связь",
    "мтс": "Жильё, ЖКХ, связь", "билайн": "Жильё, ЖКХ, связь", "мегафон": "Жильё, ЖКХ, связь",
    "интернет": "Жильё, ЖКХ, связь", "подписк": "Жильё, ЖКХ, связь", "аренда квартиры": "Жильё, ЖКХ, связь",
    "уборка": "Жильё, ЖКХ, связь",
    # дети
    "алимент": "Дети и алименты", "школ": "Дети и алименты", "сад": "Дети и алименты",
    "детск": "Дети и алименты", "ипотек": "Дети и алименты",
    # здоровье
    "аптек": "Здоровье и спорт", "клиник": "Здоровье и спорт", "врач": "Здоровье и спорт",
    "сквош": "Здоровье и спорт", "спорт": "Здоровье и спорт", "зал": "Здоровье и спорт",
    "анализ": "Здоровье и спорт",
    # красота
    "стрижк": "Красота", "барбер": "Красота", "салон": "Красота", "маникюр": "Красота",
    # покупки
    "wildberries": "Одежда и покупки",
    "wb": "Одежда и покупки", "одежд": "Одежда и покупки", "tabak": "Одежда и покупки",
    "табак": "Одежда и покупки", "днс": "Одежда и покупки", "мвидео": "Одежда и покупки",
    # развлечения
    "кино": "Развлечения", "театр": "Развлечения", "концерт": "Развлечения", "билет в": "Развлечения",
    "yandex*5814": "Кафе и доставка", "eda": "Кафе и доставка", "lavka": "Продукты",
    "bystroe": "Кафе и доставка", "pitanie": "Кафе и доставка", "coffee": "Кафе и доставка",
    "mskapt": "Здоровье и спорт", "apteka": "Здоровье и спорт",
    "pay mts": "Жильё, ЖКХ, связь", "pay.mts": "Жильё, ЖКХ, связь",
    "scooters": "Транспорт", "urent": "Транспорт", "drive": "Транспорт",
    "yandex go": "Транспорт", "каршеринг": "Транспорт",
    "yandex*go": "Транспорт", "taxi": "Транспорт", "aptek": "Здоровье и спорт",
    "pokazaniya": "Жильё, ЖКХ, связь", "билли": "Развлечения",
    # доходы
    "зарплат": "Зарплата", "аванс": "Зарплата",
    "комисс": "Комиссии ИП", "агентск": "Комиссии ИП",
    "капитализация процентов": "Проценты по счетам", "процент": "Проценты по счетам",
    # переводы
    "погашение": "Погашение карты", "погасил": "Погашение карты", "внёс на карту": "Погашение карты", "перевод между счетами": "Перевод между счетами",
    "шекел": "Покупка валюты", "usdt": "Покупка валюты", "копилк": "Пополнение копилки",
    "налог": "Налоги и взносы ИП", "усн": "Налоги и взносы ИП", "взнос": "Налоги и взносы ИП",
}

DEFAULT_RATES = {"RUB": 1.0, "ILS": 27.81, "USD": 85.71, "USDT": 85.71,
                 "EUR": 96.03, "CNY": 12.80}

def seed():
    with conn() as c:
        for name, wallet, kind, limit, sort in CATEGORIES:
            c.execute("""INSERT INTO categories(name,wallet,kind,monthly_limit,sort) VALUES(?,?,?,?,?)
                         ON CONFLICT(name) DO UPDATE SET wallet=excluded.wallet, kind=excluded.kind, sort=excluded.sort""",
                      (name, wallet, kind, limit, sort))
        for pat, cat in RULES.items():
            c.execute("INSERT OR IGNORE INTO rules(pattern,category) VALUES(?,?)", (pat, cat))
        for code, rub in DEFAULT_RATES.items():
            c.execute("INSERT OR IGNORE INTO rates(code,rub,updated) VALUES(?,?,?)",
                      (code, rub, dt.date.today().isoformat()))
        # переходный лимит на кафе в октябре
        c.execute("INSERT OR IGNORE INTO limit_overrides(month,category,monthly_limit) VALUES('2026-10','Кафе и доставка',25000)")

def rate(code):
    with conn() as c:
        r = c.execute("SELECT rub FROM rates WHERE code=?", (code.upper(),)).fetchone()
    return r["rub"] if r else 1.0

def add_txn(date, type_, amount, currency, category, account_id=None,
            to_account_id=None, note="", source="bot", ext_id=None):
    amount = abs(float(amount))
    rub = amount * rate(currency)
    with conn() as c:
        cur = c.execute("""INSERT OR IGNORE INTO txns
            (date,type,amount,currency,amount_rub,category,account_id,to_account_id,note,source,ext_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (date, type_, amount, currency.upper(), rub, category, account_id,
             to_account_id, note, source, ext_id))
        return cur.lastrowid

def account_by_name(name, create_kind=None, currency="RUB"):
    with conn() as c:
        r = c.execute("SELECT * FROM accounts WHERE lower(name)=lower(?)", (name,)).fetchone()
        if r:
            return r["id"]
        if create_kind:
            cur = c.execute("INSERT INTO accounts(name,kind,currency) VALUES(?,?,?)",
                            (name, create_kind, currency))
            return cur.lastrowid
    return None

def find_account(text):
    """Ищет счёт по названию или сокращению, упомянутому в тексте.

    «кофе 350 озон» найдёт карту «Озон», «по умолчанию яндекс» — «Яндекс дебетовая».
    """
    t = (text or "").lower()
    words = [w for w in re.findall(r"[a-zа-яё0-9]+", t) if len(w) >= 3]
    best = None
    with conn() as c:
        rows = c.execute("SELECT id,name,aliases FROM accounts WHERE active=1").fetchall()
    for r in rows:
        keys = [r["name"].lower()] + [a.strip().lower() for a in (r["aliases"] or "").split(",") if a.strip()]
        for k in keys:
            score = 0
            if len(k) >= 3 and k in t:                     # «озон» внутри сообщения
                score = len(k) + 1
            else:
                first = k.split()[0] if k.split() else ""   # «яндекс» -> «Яндекс дебетовая»
                if len(first) >= 3 and first in words:
                    score = len(first)
            if score and (best is None or score > best[1]):
                best = (r["id"], score)
    return best[0] if best else None


def month_limits(month):
    """Лимиты категорий с учётом переопределений на месяц."""
    with conn() as c:
        base = {r["name"]: r["monthly_limit"] for r in
                c.execute("SELECT name,monthly_limit FROM categories WHERE wallet='salary'")}
        for r in c.execute("SELECT category,monthly_limit FROM limit_overrides WHERE month=?", (month,)):
            base[r["category"]] = r["monthly_limit"]
    return base

LETTERS = re.compile(r"[a-zA-Zа-яёА-ЯЁ]")


def learn_rule(pattern, category):
    """Запоминает правило «кусок описания -> категория».

    Числа и короткие обрывки не запоминаем: иначе сумма «5150», записанная
    без описания, сама становится правилом и тянет за собой старую категорию.
    """
    pattern = (pattern or "").strip().lower()[:40]
    if len(pattern) < 3 or len(LETTERS.findall(pattern)) < 3:
        return
    with conn() as c:
        c.execute("""INSERT INTO rules(pattern,category,hits) VALUES(?,?,1)
                     ON CONFLICT(pattern) DO UPDATE SET category=excluded.category, hits=hits+1""",
                  (pattern, category))


def categorize(text):
    """Подбор категории по правилам. Возвращает название или None."""
    t = (text or "").lower()
    if len(LETTERS.findall(t)) < 3:        # «5150» — это сумма, а не описание
        return None
    with conn() as c:
        rows = c.execute("SELECT pattern,category FROM rules ORDER BY length(pattern) DESC").fetchall()
    for r in rows:
        if len(LETTERS.findall(r["pattern"])) < 3:
            continue
        if r["pattern"] in t:
            return r["category"]
    return None


def drop_numeric_rules():
    """Убирает мусорные правила вида «5150», накопившиеся раньше."""
    with conn() as c:
        bad = [r["pattern"] for r in c.execute("SELECT pattern FROM rules")
               if len(LETTERS.findall(r["pattern"])) < 3]
        for p in bad:
            c.execute("DELETE FROM rules WHERE pattern=?", (p,))
    return bad
