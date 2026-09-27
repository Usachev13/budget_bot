"""Проверка системы на демо-данных: разбор фраз, запись, отчёты, дашборд."""
import sys, os, datetime as dt

os.environ.setdefault("BUDGET_DB", "/tmp/selftest.db")
if os.path.exists("/tmp/selftest.db"):
    os.remove("/tmp/selftest.db")

import config
config.DASHBOARD_KEY = "test"
import db, parsing, reports, statements, app

def main():
    db.init()
    ok = True

    # 1. разбор фраз
    cases = [
        ("кофе 350", "expense", 350, "Кафе и доставка"),
        ("пятёрочка 3.2к", "expense", 3200, "Продукты"),
        ("+180000 комиссия по агентскому", "income", 180000, "Комиссии ИП"),
        ("погасил карту 20000", "transfer", 20000, "Погашение карты"),
        ("2000 шекелей купил", "transfer", 2000, "Покупка валюты"),
    ]
    for text, type_, amount, cat in cases:
        r = parsing.parse_simple(text)
        good = r and r["type"] == type_ and abs(r["amount"] - amount) < 1 and r["category"] == cat
        print(("OK  " if good else "FAIL"), text, "->", r)
        ok &= bool(good)

    # 2. запись операций
    today = dt.date.today().isoformat()
    card = db.account_by_name("Тинькофф кредитка", create_kind="card")
    acc = db.account_by_name("Основной счёт", create_kind="current")
    with db.conn() as c:
        c.execute("UPDATE accounts SET opening_balance=200000 WHERE id=?", (acc,))
        c.execute("UPDATE accounts SET opening_balance=-150000, credit_limit=385000 WHERE id=?", (card,))
    db.add_txn(today, "expense", 40000, "RUB", "Дети и алименты", account_id=acc, note="алименты")
    db.add_txn(today, "expense", 22000, "RUB", "Кафе и доставка", account_id=card, note="рестораны")
    db.add_txn(today, "income", 175000, "RUB", "Зарплата", account_id=acc, note="зарплата")
    db.add_txn(today, "income", 130000, "RUB", "Комиссии ИП", account_id=acc, note="комиссия")
    db.add_txn(today, "transfer", 50000, "RUB", "Погашение карты", account_id=acc, to_account_id=card)

    md = reports.month_data()
    cafe = next(i for i in md["salary"] if i["name"] == "Кафе и доставка")
    over = cafe["fact"] > cafe["limit"]
    print(("OK  " if over else "FAIL"), "перерасход по кафе виден:", round(cafe["fact"]), ">", cafe["limit"])
    ok &= over

    np_ = reports.net_position()
    print("     чистая позиция:", round(np_["net"]), "= свои", round(np_["own"]), "− долг", round(np_["debt"]))
    ok &= abs(np_["net"] - (415000 - 122000)) < 1

    split = reports.ip_split_plan(md["ip_income"])
    print("     распределение ИП:", {k: round(v) for k, v in split.items()})
    ok &= abs(sum(split.values()) - md["ip_income"]) < 1

    # 3. дашборд отвечает
    client = app.app.test_client()
    r = client.get("/dashboard?key=test")
    print(("OK  " if r.status_code == 200 else "FAIL"), "дашборд:", r.status_code, len(r.data), "байт")
    ok &= r.status_code == 200
    r2 = client.get("/dashboard?key=wrong")
    print(("OK  " if r2.status_code == 403 else "FAIL"), "чужой ключ отклонён:", r2.status_code)
    ok &= r2.status_code == 403

    print("\nИТОГ:", "всё работает" if ok else "есть ошибки")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
