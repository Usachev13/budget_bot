"""Импорт выписок: PDF Яндекс Банка и CSV/TSV любого банка."""
import re, io, csv, hashlib, datetime as dt
import db

MONEY = r"([+\-–−]?\s?[\d  ]+,\d\d)\s*₽"
DATE = r"(\d\d\.\d\d\.\d{4})"

def _num(s):
    s = s.replace(" ", " ").replace("–", "-").replace("−", "-").replace(" ", "").replace(",", ".")
    return float(s)

def _ext_id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]

def _iso(d):
    return dt.datetime.strptime(d, "%d.%m.%Y").date().isoformat()

# ------------------------------------------------------------------ PDF
def pdf_text(data):
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((p.extract_text(layout=True) or "") for p in pdf.pages)
    except ImportError:
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)

def parse_yandex_pdf(data, filename=""):
    """Возвращает (список операций, имя счёта, остаток на конец)."""
    text = pdf_text(data)
    acc = re.search(r"№ ([НК]\d{15,})", text)
    contract = acc.group(1) if acc else filename
    kind = "savings" if contract.startswith("Н") else "current"
    name_m = re.search(r'продукта\s*«?([^»\n]+)', text)
    account_name = f"Яндекс · {'Сейв' if kind == 'savings' else 'счёт'} {contract[-6:]}"
    closing = re.search(r"Исходящий остаток за " + DATE + r"\s+([\d  ]+,\d\d) ₽", text)
    closing_date, closing_val = (_iso(closing.group(1)), _num(closing.group(2))) if closing else (None, None)

    ops = []
    lines = text.splitlines()
    SKIP = ("Входящий остаток", "Исходящий остаток", "Итого списаний", "Итого зачислений",
            "Описание", "Страница", "Продолжение", "Выписка")
    for i, line in enumerate(lines):
        dm = re.search(DATE, line)
        amounts = re.findall(MONEY, line)
        if not dm or not amounts:
            continue
        desc = line[:dm.start()].strip()
        if not desc or desc.startswith(SKIP):
            continue
        date, amount = _iso(dm.group(1)), _num(amounts[0])
        # название магазина часто переносится на следующую строку
        if i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            tail = re.split(r"\s{2,}", nxt)[0] if nxt else ""
            if tail and not tail.startswith("в ") and not re.search(DATE, nxt) \
               and "₽" not in nxt and not tail.startswith(SKIP):
                desc = f"{desc} {tail}".strip()
        ops.append({"date": date, "desc": desc, "amount": amount, "contract": contract})
    return ops, account_name, kind, closing_date, closing_val

# ------------------------------------------------------------------ CSV
def parse_csv(data, filename=""):
    text = data.decode("utf-8-sig", errors="replace")
    dialect = csv.Sniffer().sniff(text[:2000], delimiters=";,\t")
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
    return ops

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
            exists = c.execute("SELECT 1 FROM txns WHERE ext_id=?", (ext,)).fetchone()
        if exists:
            skipped += 1
            continue
        if not cat:
            unknown += 1        # категорию спросим в боте командой /разобрать
        db.add_txn(o["date"], type_, abs(o["amount"]), "RUB", cat,
                   account_id=account_id if type_ != "income" else account_id,
                   note=o["desc"][:120], source=source, ext_id=ext)
        added += 1
    return added, skipped, unknown

def import_bytes(data, filename=""):
    """Точка входа для бота: определяет формат и грузит."""
    if filename.lower().endswith(".pdf") or data[:4] == b"%PDF":
        ops, acc_name, kind, cdate, cval = parse_yandex_pdf(data, filename)
        acc_id = db.account_by_name(acc_name, create_kind=kind)
        added, skipped, unknown = import_ops(ops, acc_id)
        if cdate and cval is not None:
            with db.conn() as c:
                c.execute("INSERT OR REPLACE INTO balances(account_id,date,balance) VALUES(?,?,?)",
                          (acc_id, cdate, cval))
        report = f"Счёт: {acc_name}"
        if cval is not None:
            report += f", остаток на {cdate}: {cval:,.0f} ₽".replace(",", " ")
        if unknown:
            report += f"\nБез категории (ушли в «Непредвиденное»): {unknown}"
        return added, skipped, report
    ops = parse_csv(data, filename)
    acc_id = db.account_by_name(f"Импорт · {filename[:20]}", create_kind="current")
    added, skipped, unknown = import_ops(ops, acc_id)
    return added, skipped, f"Файл {filename}: без категории {unknown}"
