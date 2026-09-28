"""Синхронизация справочников с Google Таблицей.

Таблица — источник правды: категории, лимиты, правила автокатегоризации, счета.
Читаем через gviz-выгрузку в CSV, для этого таблице нужен доступ «по ссылке — читатель».
Никаких токенов и OAuth не требуется.
"""
import csv, io, re
import requests
import config, db

GVIZ = "https://docs.google.com/spreadsheets/d/{id}/gviz/tq?tqx=out:csv&sheet={sheet}"

WALLET = {"зарплата": "salary", "ип": "ip", "доход": "income", "перевод": "transfer"}
KIND = {"расход": "expense", "доход": "income", "перевод": "transfer"}
ACC_KIND = {"текущий": "current", "накопительный": "savings",
            "карта": "card", "валютный": "wallet"}


def fetch(sheet_name, sheet_id=None):
    """Возвращает список строк листа как словари по заголовкам первой строки."""
    sheet_id = sheet_id or config.SHEET_ID
    if not sheet_id:
        raise RuntimeError("Не задан SHEET_ID: пропиши его в .budget_env")
    url = GVIZ.format(id=sheet_id, sheet=requests.utils.quote(sheet_name))
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    if text.lstrip().startswith("<"):
        raise RuntimeError("Таблица закрыта: открой доступ «по ссылке — читатель»")
    rows = list(csv.DictReader(io.StringIO(text)))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in rows]


def _num(v):
    v = re.sub(r"[^\d,.\-]", "", v or "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return 0.0


def sync_categories(sheet_id=None):
    rows = fetch("Категории", sheet_id)
    seen, sort = [], 10
    with db.conn() as c:
        for r in rows:
            name = r.get("Категория", "")
            if not name:
                continue
            wallet = WALLET.get(r.get("Кошелёк", "").lower(), "salary")
            kind = KIND.get(r.get("Тип", "").lower(), "expense")
            limit = _num(r.get("Лимит ₽/мес", ""))
            c.execute("""INSERT INTO categories(name,wallet,kind,monthly_limit,sort)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(name) DO UPDATE SET wallet=excluded.wallet,
                           kind=excluded.kind, monthly_limit=excluded.monthly_limit,
                           sort=excluded.sort""", (name, wallet, kind, limit, sort))
            seen.append(name)
            sort += 10
        # категории, которых больше нет в таблице, удаляем, если по ним нет операций
        for row in c.execute("SELECT name FROM categories").fetchall():
            if row["name"] in seen:
                continue
            used = c.execute("SELECT 1 FROM txns WHERE category=? LIMIT 1", (row["name"],)).fetchone()
            if not used:
                c.execute("DELETE FROM categories WHERE name=?", (row["name"],))
    return len(seen)


def sync_rules(sheet_id=None):
    rows = fetch("Правила", sheet_id)
    with db.conn() as c:
        known = {r["name"] for r in c.execute("SELECT name FROM categories")}
        c.execute("DELETE FROM rules")
        n = 0
        for r in rows:
            pattern = (r.get("Текст в описании операции") or "").strip().lower()
            category = (r.get("Категория") or "").strip()
            if not pattern or category not in known:
                continue
            c.execute("INSERT OR REPLACE INTO rules(pattern,category) VALUES(?,?)",
                      (pattern, category))
            n += 1
    return n


def sync_accounts(sheet_id=None):
    rows = fetch("Счета", sheet_id)
    n = 0
    with db.conn() as c:
        for r in rows:
            name = r.get("Название", "")
            if not name:
                continue
            kind = ACC_KIND.get(r.get("Тип", "").lower(), "current")
            currency = (r.get("Валюта") or "RUB").upper()
            purpose = r.get("Назначение", "")
            opening = _num(r.get("Остаток на старте", ""))
            limit = _num(r.get("Лимит по карте", ""))
            c.execute("""INSERT INTO accounts(name,kind,currency,purpose,opening_balance,credit_limit)
                         VALUES(?,?,?,?,?,?)
                         ON CONFLICT(name) DO UPDATE SET kind=excluded.kind,
                           currency=excluded.currency, purpose=excluded.purpose,
                           credit_limit=excluded.credit_limit""",
                      (name, kind, currency, purpose, opening, limit))
            n += 1
    return n


def sync_all(sheet_id=None):
    cats = sync_categories(sheet_id)
    rules = sync_rules(sheet_id)
    try:
        accounts = sync_accounts(sheet_id)
    except Exception:
        accounts = 0          # лист «Счета» необязателен
    with db.conn() as c:
        limit = c.execute("SELECT COALESCE(SUM(monthly_limit),0) s FROM categories WHERE wallet='salary'").fetchone()["s"]
    return {"categories": cats, "rules": rules, "accounts": accounts, "salary_limit": limit}
