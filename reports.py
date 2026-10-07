"""Подсчёты для бота и дашборда."""
import re, datetime as dt
import config, db

def month_key(d=None):
    d = d or dt.date.today()
    return d.strftime("%Y-%m")

def month_bounds(month):
    y, m = map(int, month.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1)
    return start.isoformat(), end.isoformat()

# ------------------------------------------------------------------ остатки
def balances():
    """Остаток по каждому счёту: последний снимок плюс операции после него."""
    out = []
    with db.conn() as c:
        for a in c.execute("SELECT * FROM accounts WHERE active=1 ORDER BY kind,name"):
            snap = c.execute("SELECT date,balance FROM balances WHERE account_id=? ORDER BY date DESC LIMIT 1",
                             (a["id"],)).fetchone()
            base, since = (snap["balance"], snap["date"]) if snap else (a["opening_balance"], a["opening_date"])
            q = "SELECT type,amount,account_id,to_account_id FROM txns WHERE date>? AND (account_id=? OR to_account_id=?)"
            delta = 0.0
            for t in c.execute(q, (since, a["id"], a["id"])):
                if t["type"] == "income" and t["account_id"] == a["id"]:
                    delta += t["amount"]
                elif t["type"] == "expense" and t["account_id"] == a["id"]:
                    delta -= t["amount"]
                elif t["type"] == "transfer":
                    if t["account_id"] == a["id"]:
                        delta -= t["amount"]
                    if t["to_account_id"] == a["id"]:
                        delta += t["amount"]
            out.append({"id": a["id"], "name": a["name"], "kind": a["kind"],
                        "currency": a["currency"], "purpose": a["purpose"],
                        "balance": base + delta,
                        "rub": (base + delta) * db.rate(a["currency"]),
                        "credit_limit": a["credit_limit"]})
    return out

def net_position():
    """Свои деньги минус долг по картам.

    Отдельно считаем позицию без копилки переезда: эти деньги отложены
    на отъезд и на погашение карт не идут.
    """
    own = debt = relocation_rub = 0.0
    for a in balances():
        if a["kind"] == "card":
            debt += max(0.0, -a["rub"])
            continue
        own += a["rub"]
        if (a["purpose"] or "").lower().startswith("переезд"):
            relocation_rub += a["rub"]
    return {"own": own, "debt": debt, "net": own - debt,
            "relocation": relocation_rub,
            "own_wo_relocation": own - relocation_rub,
            "net_wo_relocation": own - relocation_rub - debt}


# --------------------------------------------------------------- по месяцам
def month_data(month=None):
    month = month or month_key()
    start, end = month_bounds(month)
    limits = db.month_limits(month)
    with db.conn() as c:
        cats = {r["name"]: r for r in c.execute("SELECT * FROM categories")}
        rows = c.execute("""SELECT category, type, SUM(amount_rub) s FROM txns
                            WHERE date BETWEEN ? AND ? GROUP BY category, type""", (start, end)).fetchall()
    fact = {r["category"]: r["s"] for r in rows}
    salary, ip, income = [], [], []
    for name, row in sorted(cats.items(), key=lambda kv: kv[1]["sort"]):
        s = fact.get(name, 0.0)
        item = {"name": name, "fact": s, "limit": limits.get(name, row["monthly_limit"])}
        if row["wallet"] == "salary":
            salary.append(item)
        elif row["wallet"] == "ip":
            ip.append(item)
        elif row["wallet"] == "income" and s:
            income.append(item)
    salary_fact = sum(i["fact"] for i in salary)
    salary_limit = sum(i["limit"] for i in salary) or config.SALARY_BUDGET
    ip_income = sum(i["fact"] for i in income if i["name"] in ("Комиссии ИП", "Проценты по счетам", "Прочие доходы"))
    return {"month": month, "salary": salary, "ip": ip, "income": income,
            "salary_fact": salary_fact, "salary_limit": salary_limit,
            "ip_income": ip_income, "ip_spent": sum(i["fact"] for i in ip),
            "total_income": sum(i["fact"] for i in income)}

def trend(n=12):
    """Доходы и расходы по месяцам, ₽."""
    today = dt.date.today().replace(day=1)
    months = []
    for i in range(n - 1, -1, -1):
        y, m = divmod((today.year * 12 + today.month - 1) - i, 12)
        months.append(f"{y:04d}-{m + 1:02d}")
    out = []
    with db.conn() as c:
        for mo in months:
            s, e = month_bounds(mo)
            inc = c.execute("SELECT COALESCE(SUM(amount_rub),0) v FROM txns WHERE type='income' AND date BETWEEN ? AND ?", (s, e)).fetchone()["v"]
            exp = c.execute("SELECT COALESCE(SUM(amount_rub),0) v FROM txns WHERE type='expense' AND date BETWEEN ? AND ?", (s, e)).fetchone()["v"]
            out.append({"month": mo, "income": inc, "expense": exp})
    return out

def ip_split_plan(amount):
    """Как распределить деньги ИП по долям."""
    np_ = net_position()["net"]
    shares = dict(config.IP_SPLIT)
    if np_ >= 0:                       # разрыв закрыт — доля уходит в инвестиции
        shares["Инвестиции"] = shares.pop("Закрытие разрыва")
    return {k: amount * v for k, v in shares.items()}

def relocation():
    """Прогресс по переезду: всё, что лежит на счетах с назначением «переезд», в шекелях."""
    ils_rate = db.rate("ILS") or 1
    have = 0.0
    parts = []
    for a in balances():
        if (a["purpose"] or "").lower().startswith("переезд"):
            in_ils = a["rub"] / ils_rate
            have += in_ils
            parts.append({"name": a["name"], "amount": a["balance"],
                          "currency": a["currency"], "ils": in_ils})
    left_days = (dt.date.fromisoformat(config.RELOCATION_DEADLINE) - dt.date.today()).days
    months_left = max(1, round(left_days / 30))
    need = max(0.0, config.RELOCATION_GOAL_ILS - have)
    return {"have": have, "goal": config.RELOCATION_GOAL_ILS, "need": need,
            "months_left": months_left, "per_month": need / months_left,
            "per_month_rub": need / months_left * ils_rate, "parts": parts}

MERCHANT_STRIP = re.compile(r"[\d№#]+[\d\-/.]*")
STOP_TAIL = ("заказ", "без ндс", "номер", "перевод по сбп", "покупка")


def merchant_key(note):
    """Ключ продавца: описание без номеров заказов, хвостов и лишних знаков."""
    s = (note or "").lower()
    for stop in STOP_TAIL:
        s = s.split(stop)[0]
    s = MERCHANT_STRIP.sub("", s)
    s = re.sub(r"[^a-zа-яё ]+", " ", s)
    words = [w for w in s.split() if len(w) > 1]
    return " ".join(words)[:40] or "без описания"


def unsorted_groups():
    """Неразобранные операции, сгруппированные по продавцу. Самая крупная группа первой."""
    with db.conn() as c:
        rows = c.execute("SELECT id, date, amount_rub, note, type FROM txns WHERE category=''").fetchall()
    groups = {}
    for r in rows:
        g = groups.setdefault(merchant_key(r["note"]),
                              {"ids": [], "sum": 0.0, "note": r["note"], "date": r["date"]})
        g["ids"].append(r["id"])
        g["sum"] += r["amount_rub"]
    out = [{"key": k, **v, "count": len(v["ids"])} for k, v in groups.items()]
    return sorted(out, key=lambda g: (-g["count"], -g["sum"]))


def apply_category_to_group(txn_ids, category):
    with db.conn() as c:
        kind = c.execute("SELECT kind FROM categories WHERE name=?", (category,)).fetchone()
        c.executemany("UPDATE txns SET category=?, type=COALESCE(?,type) WHERE id=?",
                      [(category, kind["kind"] if kind else None, i) for i in txn_ids])


def unsorted_txns(limit=10):
    """Операции без категории — их бот предлагает разобрать."""
    with db.conn() as c:
        return [dict(x) for x in c.execute(
            "SELECT id,date,amount_rub,note,type FROM txns WHERE category='' ORDER BY amount_rub DESC LIMIT ?",
            (limit,))]


def unsorted_count():
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) n FROM txns WHERE category=''").fetchone()["n"]


def categories_for(type_):
    with db.conn() as c:
        return [r["name"] for r in c.execute(
            "SELECT name FROM categories WHERE kind=? ORDER BY sort", (type_,))]

def category_left(category):
    md = month_data()
    for item in md["salary"]:
        if item["name"] == category:
            return item["limit"] - item["fact"]
    return None

# ------------------------------------------------------------------- тексты
def _m(v):
    return f"{v:,.0f}".replace(",", " ") + " ₽"

def month_summary_text():
    md = month_data()
    np_ = net_position()
    lines = [f"<b>Месяц {md['month']}</b>",
             f"Из зарплаты: {_m(md['salary_fact'])} из {_m(md['salary_limit'])}",
             ""]
    for i in md["salary"]:
        if not i["limit"] and not i["fact"]:
            continue
        mark = "❗️" if i["fact"] > i["limit"] > 0 else "·"
        lines.append(f"{mark} {i['name']}: {_m(i['fact'])} из {_m(i['limit'])}")
    lines += ["", f"Доходы: {_m(md['total_income'])}",
              f"Потрачено из ИП: {_m(md['ip_spent'])}",
              f"Свои деньги: {_m(np_['own'])} · долг по картам: {_m(np_['debt'])}",
              f"Чистая позиция: {_m(np_['net'])}"]
    if np_.get("relocation"):
        lines.append(f"Без копилки переезда: {_m(np_['net_wo_relocation'])} "
                     f"(отложено {_m(np_['relocation'])})")
    r = relocation()
    if r["goal"]:
        lines.append(f"Переезд: {r['have']:,.0f} из {r['goal']:,.0f} ₪, "
                     f"нужно {r['per_month']:,.0f} ₪/мес".replace(",", " "))
    return "\n".join(lines)

def accounts_text():
    lines = ["<b>Счета</b>"]
    for a in balances():
        sym = {"RUB": "₽", "ILS": "₪", "USD": "$", "USDT": "USDT"}.get(a["currency"], a["currency"])
        v = f"{a['balance']:,.0f}".replace(",", " ")
        tag = " (карта)" if a["kind"] == "card" else ""
        lines.append(f"· {a['name']}{tag}: {v} {sym}")
    np_ = net_position()
    lines.append(f"\nЧистая позиция: {_m(np_['net'])}")
    return "\n".join(lines)

def today_text():
    today = dt.date.today().isoformat()
    with db.conn() as c:
        rows = c.execute("""SELECT category, amount_rub, note FROM txns
                            WHERE date=? AND type='expense' ORDER BY id""", (today,)).fetchall()
    if not rows:
        return "Сегодня трат нет."
    total = sum(r["amount_rub"] for r in rows)
    lines = [f"<b>Сегодня: {_m(total)}</b>"]
    for r in rows:
        lines.append(f"· {r['note'] or r['category']} — {_m(r['amount_rub'])} ({r['category']})")
    return "\n".join(lines)
