"""Импорт выписок.

Поддержаны PDF Яндекс Банка и Озон Банка, а также CSV/TSV любого банка.
Новый банк добавляется отдельной функцией parse_* и строкой в detect_bank.
"""
import re, io, csv, hashlib, datetime as dt
import db

# суммы: «1 234,56 ₽» у Яндекса и «- 1 234.00 ₽» у Озона
MONEY = r"([+\-–−]?\s?[\d \u00a0]+[.,]\d\d)\s*₽"
DATE = r"(\d\d\.\d\d\.\d{4})"


def _num(s):
    s = (s.replace("\u00a0", " ").replace("–", "-").replace("−", "-")
          .replace(" ", "").replace(",", "."))
    return float(s)


def _ext_id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


def _iso(d):
    return dt.datetime.strptime(d, "%d.%m.%Y").date().isoformat()


def pdf_text(data):
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((p.extract_text(layout=True) or "") for p in pdf.pages)
    except ImportError:
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)


def detect_bank(text):
    up = text.upper()
    if "ЯНДЕКС БАНК" in up or "YABANK" in up:
        return "yandex"
    if "ОЗОН БАНК" in up or "OZON БАНК" in up or "ОЗОН-БАНК" in up:
        return "ozon"
    return "generic"


# ------------------------------------------------------------------ Яндекс
def parse_yandex(text, filename=""):
    acc = re.search(r"№ ([НК]\d{15,})", text)
    contract = acc.group(1) if acc else filename
    kind = "savings" if contract.startswith("Н") else "current"
    account_name = f"Яндекс · {'Сейв' if kind == 'savings' else 'счёт'} {contract[-6:]}"
    closing = re.search(r"Исходящий остаток за " + DATE + r"\s+([\d \u00a0]+,\d\d) ₽", text)
    closing_date, closing_val = (_iso(closing.group(1)), _num(closing.group(2))) if closing else (None, None)

    SKIP = ("Входящий остаток", "Исходящий остаток", "Итого списаний", "Итого зачислений",
            "Описание", "Страница", "Продолжение", "Выписка")
    ops, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        dm = re.search(DATE, line)
        amounts = re.findall(MONEY, line)
        if not dm or not amounts:
            continue
        desc = line[:dm.start()].strip()
        if not desc or desc.startswith(SKIP):
            continue
        # название магазина часто переносится на следующую строку
        if i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            tail = re.split(r"\s{2,}", nxt)[0] if nxt else ""
            if tail and not tail.startswith("в ") and not re.search(DATE, nxt) \
               and "₽" not in nxt and not tail.startswith(SKIP):
                desc = f"{desc} {tail}".strip()
        ops.append({"date": _iso(dm.group(1)), "desc": desc,
                    "amount": _num(amounts[0]), "contract": contract})
    return {"ops": ops, "account": account_name, "kind": kind,
            "closing_date": closing_date, "closing": closing_val}


# -------------------------------------------------------------------- Озон
OZON_ROW = re.compile(r"^\s*" + DATE + r"\s+\d\d:\d\d:\d\d\s+(\S+)\s+(.*)$")


def parse_ozon(text, filename=""):
    """Справка о движении средств Озон Банка: дата со временем, назначение в несколько строк."""
    c = re.search(r"Номер договора:\s*№?\s*(\S+)", text)
    contract = c.group(1) if c else filename
    kind = "card" if "-KK-" in contract.upper() else "current"
    account_name = f"Озон · {'карта' if kind == 'card' else 'счёт'} {contract[-6:]}"

    ops, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        m = OZON_ROW.match(line)
        if not m:
            continue
        rest = m.group(3)
        amounts = re.findall(MONEY, rest)
        if not amounts:
            continue
        desc = rest[:rest.find(amounts[0].strip()[:4])].strip() if amounts[0].strip()[:4] in rest else rest
        desc = re.sub(MONEY, "", desc).strip(" .")
        # назначение платежа продолжается на следующих строках
        for nxt in lines[i + 1:i + 5]:
            t = nxt.strip()
            if not t or OZON_ROW.match(nxt) or "₽" in t:
                break
            # слева может переноситься номер документа, справа — текст назначения
            for piece in re.split(r"\s{2,}", t):
                if not piece or piece.isdigit() or piece.startswith("Без НДС"):
                    continue
                desc = f"{desc} {piece}".strip()
        ops.append({"date": _iso(m.group(1)), "desc": desc[:160],
                    "amount": _num(amounts[0]), "contract": contract})

    spent = re.search(r"Итого списаний за период:\s*([\d \u00a0]+[.,]\d\d)", text)
    got = re.search(r"Итого зачислений за период:\s*([\d \u00a0]+[.,]\d\d)", text)
    return {"ops": ops, "account": account_name, "kind": kind,
            "closing_date": None, "closing": None,
            "totals": {"spent": _num(spent.group(1)) if spent else None,
                       "got": _num(got.group(1)) if got else None}}


# ----------------------------------------------------------------- прочее
def parse_generic(text, filename=""):
    """Запасной разбор: любая строка с датой и суммой в рублях."""
    ops = []
    for line in text.splitlines():
        dm = re.search(DATE, line)
        amounts = re.findall(MONEY, line)
        if not dm or not amounts:
            continue
        desc = re.sub(MONEY, "", line[:dm.start()] + " " + line[dm.end():]).strip()
        ops.append({"date": _iso(dm.group(1)), "desc": desc[:160],
                    "amount": _num(amounts[0]), "contract": filename})
    return {"ops": ops, "account": f"Импорт · {filename[:20]}", "kind": "current",
            "closing_date": None, "closing": None}


def parse_csv(data, filename=""):
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    ops = []
    for r in rows:
        keys = {k.lower().strip(): (v or "").strip() for k, v in r.items() if k}
        date = next((v for k, v in keys.items() if "дата" in k or "date" in k), "")
        amount = next((v for k, v in keys.items() if "сумма" in k or "amount" in k), "")
        desc = next((v for k, v in keys.items() if "опис" in k or "назнач" in k or "категор" in k
                     or "desc" in k or "merchant" in k), "")
        if not date or not amount:
            continue
        try:
            d = _iso(date[:10]) if "." in date else date[:10]
            a = _num(amount.replace("₽", ""))
        except Exception:
            continue
        ops.append({"date": d, "desc": desc, "amount": a, "contract": filename})
    return {"ops": ops, "account": f"Импорт · {filename[:20]}", "kind": "current",
            "closing_date": None, "closing": None}


# --------------------------------------------------------------- сохранение
SELF_TRANSFER = ("перевод между счетами", "перевод себе", "между своими")


def classify(desc, amount):
    """Тип операции и категория по описанию."""
    low = desc.lower()
    if any(w in low for w in SELF_TRANSFER):
        return "transfer", "Перевод между счетами"
    if "капитализация процентов" in low:
        return "income", "Проценты по счетам"
    if "погашение основного долга" in low or "погашение кредита" in low:
        return "transfer", "Погашение карты"
    cat = db.categorize(low)
    if cat:
        with db.conn() as c:
            row = c.execute("SELECT kind FROM categories WHERE name=?", (cat,)).fetchone()
        if row:
            return row["kind"], cat
    return ("income", "Прочие доходы") if amount > 0 else ("expense", "")


def import_ops(ops, account_id, source="statement"):
    added = skipped = unknown = 0
    for o in ops:
        type_, cat = classify(o["desc"], o["amount"])
        ext = _ext_id(o["contract"], o["date"], o["desc"][:40], o["amount"])
        with db.conn() as c:
            if c.execute("SELECT 1 FROM txns WHERE ext_id=?", (ext,)).fetchone():
                skipped += 1
                continue
        if not cat:
            unknown += 1        # категорию спросим в боте командой /разобрать
        db.add_txn(o["date"], type_, abs(o["amount"]), "RUB", cat,
                   account_id=account_id, note=o["desc"][:120], source=source, ext_id=ext)
        added += 1
    return added, skipped, unknown


def parse_pdf(data, filename=""):
    text = pdf_text(data)
    bank = detect_bank(text)
    parser = {"yandex": parse_yandex, "ozon": parse_ozon}.get(bank, parse_generic)
    result = parser(text, filename)
    result["bank"] = bank
    return result


def import_bytes(data, filename=""):
    """Точка входа для бота: определяет формат и грузит."""
    if filename.lower().endswith(".pdf") or data[:4] == b"%PDF":
        res = parse_pdf(data, filename)
    else:
        res = parse_csv(data, filename)
        res["bank"] = "csv"

    acc_id = db.account_by_name(res["account"], create_kind=res["kind"])
    added, skipped, unknown = import_ops(res["ops"], acc_id)

    if res.get("closing_date") and res.get("closing") is not None:
        with db.conn() as c:
            c.execute("INSERT OR REPLACE INTO balances(account_id,date,balance) VALUES(?,?,?)",
                      (acc_id, res["closing_date"], res["closing"]))

    report = f"Счёт: {res['account']}"
    if res.get("closing") is not None:
        report += f", остаток на {res['closing_date']}: {res['closing']:,.0f} ₽".replace(",", " ")
    totals = res.get("totals") or {}
    if totals.get("spent"):
        report += f"\nСписаний за период: {totals['spent']:,.0f} ₽".replace(",", " ")
    if res["kind"] == "card":
        report += "\nЭто кредитная карта: пришли текущую задолженность командой «долг <сумма>», если знаешь её."
    if unknown:
        report += f"\nБез категории: {unknown} — разбери командой /разобрать"
    if not added and not skipped:
        report += "\nОпераций не нашёл. Пришли файл целиком, без выделения страниц."
    return added, skipped, report
