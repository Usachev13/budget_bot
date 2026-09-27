"""Разбор сообщений: «кофе 350», «+180000 комиссия», «перевод 50000 на шекели».
Сначала простые правила, если не вышло — модель Groq."""
import re, json, datetime as dt
import requests
import config, db

CUR_WORDS = {
    "р": "RUB", "руб": "RUB", "рублей": "RUB", "₽": "RUB", "r": "RUB",
    "ш": "ILS", "шек": "ILS", "шекель": "ILS", "шекелей": "ILS", "₪": "ILS", "ils": "ILS",
    "$": "USD", "долл": "USD", "usd": "USD", "долларов": "USD",
    "usdt": "USDT", "тезер": "USDT",
}
TRANSFER_WORDS = ("перевод", "погаш", "закинул", "положил на", "копилк", "купил шекел",
                  "обмен", "перевёл себе", "перевел себе")
INCOME_WORDS = ("получил", "зарплата", "аванс", "комисси", "вернули", "процент", "доход",
                "поступил", "пришло")

AMOUNT_RE = re.compile(r"(?<![\d,.])(\d[\d  ]*(?:[.,]\d{1,2})?)\s*(к|k|тыс|т)?\b", re.I)

def _num(raw, mult):
    v = float(raw.replace(" ", "").replace(" ", "").replace(",", "."))
    if mult and mult.lower() in ("к", "k", "тыс", "т"):
        v *= 1000
    return v

def parse_simple(text):
    """Возвращает dict или None. Формат: {type, amount, currency, category, note}"""
    t = text.strip()
    if not t:
        return None
    low = t.lower()
    m = AMOUNT_RE.search(low)
    if not m:
        return None
    amount = _num(m.group(1), m.group(2))
    if amount <= 0:
        return None

    currency = "RUB"
    tail = low[m.end():m.end() + 12]
    for w, code in CUR_WORDS.items():
        if tail.strip().startswith(w) or f" {w} " in f" {low} ":
            currency = code
            break

    if t.lstrip().startswith("+") or any(w in low for w in INCOME_WORDS):
        type_ = "income"
    elif any(w in low for w in TRANSFER_WORDS):
        type_ = "transfer"
    else:
        type_ = "expense"

    note = AMOUNT_RE.sub("", t, count=1).strip(" +-,.;:")
    note = re.sub(r"\s+(р|руб|рублей|₽|шек|шекелей|₪|usd|usdt|долларов)\b", "", note, flags=re.I).strip()
    category = db.categorize(low)
    if category:
        # тип операции определяет категория: покупка валюты — это перевод, а не расход
        with db.conn() as c:
            row = c.execute("SELECT kind FROM categories WHERE name=?", (category,)).fetchone()
        if row:
            type_ = row["kind"]
    else:
        if type_ == "income":
            category = "Прочие доходы"
        elif type_ == "transfer":
            category = "Перевод между счетами"
    return {"type": type_, "amount": amount, "currency": currency,
            "category": category, "note": note or t, "date": dt.date.today().isoformat()}

# ------------------------------------------------------------------ Groq
def _groq(path, **kw):
    if not config.GROQ_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
    try:
        r = requests.post(f"https://api.groq.com/openai/v1/{path}", headers=headers, timeout=60, **kw)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("groq error:", e)
        return None

def transcribe(audio_bytes, filename="voice.ogg"):
    """Голосовое -> текст."""
    data = _groq("audio/transcriptions",
                 files={"file": (filename, audio_bytes, "audio/ogg")},
                 data={"model": config.GROQ_STT_MODEL, "language": "ru"})
    return (data or {}).get("text", "").strip()

def parse_llm(text, categories):
    """Разбор сложной фразы моделью. Возвращает dict или None."""
    prompt = (
        "Ты разбираешь записи о личных финансах на русском. Верни только JSON без пояснений: "
        '{"type":"expense|income|transfer","amount":число,"currency":"RUB|ILS|USD|USDT",'
        '"category":"одна из списка","note":"краткое описание"}. '
        f"Категории: {', '.join(categories)}. Запись: " + text
    )
    data = _groq("chat/completions", json={
        "model": config.GROQ_LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    })
    if not data:
        return None
    try:
        obj = json.loads(data["choices"][0]["message"]["content"])
        obj["amount"] = float(obj["amount"])
        obj.setdefault("date", dt.date.today().isoformat())
        return obj
    except Exception:
        return None

def parse(text):
    res = parse_simple(text)
    if res and res.get("category"):
        return res
    with db.conn() as c:
        cats = [r["name"] for r in c.execute("SELECT name FROM categories ORDER BY sort")]
    llm = parse_llm(text, cats)
    if llm and llm.get("category") in cats:
        if res:
            llm.setdefault("note", res["note"])
        return llm
    return res
