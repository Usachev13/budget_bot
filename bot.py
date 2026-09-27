"""Логика телеграм-бота. Работает через webhook, без сторонних библиотек."""
import json, re, datetime as dt, io
import requests
import config, db, parsing, reports

API = "https://api.telegram.org/bot{}/{}"

def call(method, **payload):
    if not config.TELEGRAM_TOKEN:
        return None
    try:
        r = requests.post(API.format(config.TELEGRAM_TOKEN, method), json=payload, timeout=30)
        return r.json()
    except Exception as e:
        print("telegram error:", e)
        return None

def send(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return call("sendMessage", **payload)

def get_file(file_id):
    r = requests.get(API.format(config.TELEGRAM_TOKEN, "getFile"),
                     params={"file_id": file_id}, timeout=30).json()
    path = r["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{config.TELEGRAM_TOKEN}/{path}"
    return requests.get(url, timeout=120).content, path

def money(v, cur="RUB"):
    sym = {"RUB": "₽", "ILS": "₪", "USD": "$", "USDT": "USDT"}.get(cur, cur)
    return f"{v:,.0f}".replace(",", " ") + " " + sym

# ----------------------------------------------------------------- сообщения
HELP = (
    "<b>Как пользоваться</b>\n"
    "Просто пиши операции: «кофе 350», «пятёрочка 3.2к», «+180000 комиссия», "
    "«перевод 55500 на шекели». Можно голосом.\n"
    "Выписку в PDF или CSV пришли файлом — разберу и загружу.\n\n"
    "<b>Команды</b>\n"
    "/итоги — месяц: лимиты, остатки, чистая позиция\n"
    "/счета — остатки по счетам\n"
    "/день — траты за сегодня\n"
    "/разобрать — присвоить категории операциям из выписок\n"
    "долг 45000 — записать текущую задолженность по карте\n"
    "/отмена — удалить последнюю операцию\n"
    "/дашборд — ссылка на дашборд"
)

def handle_update(update):
    msg = update.get("message") or update.get("edited_message")
    cb = update.get("callback_query")
    if cb:
        return handle_callback(cb)
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    if config.OWNER_CHAT_ID and chat_id != config.OWNER_CHAT_ID:
        return send(chat_id, "Этот бот личный.")

    if "voice" in msg or "audio" in msg:
        f = msg.get("voice") or msg.get("audio")
        audio, _ = get_file(f["file_id"])
        text = parsing.transcribe(audio)
        if not text:
            return send(chat_id, "Не разобрал голосовое. Напиши текстом.")
        send(chat_id, f"🎧 {text}")
        return handle_text(chat_id, text)

    if "document" in msg:
        return handle_document(chat_id, msg["document"])

    text = (msg.get("text") or "").strip()
    if not text:
        return
    return handle_text(chat_id, text)

def handle_text(chat_id, text):
    low = text.lower().lstrip("/")
    if low in ("start", "help", "помощь"):
        return send(chat_id, HELP)
    if low.startswith(("итоги", "month", "отчет", "отчёт")):
        return send(chat_id, reports.month_summary_text())
    if low.startswith(("счета", "баланс")):
        return send(chat_id, reports.accounts_text())
    if low.startswith("день"):
        return send(chat_id, reports.today_text())
    if low.startswith(("дашборд", "dashboard")):
        return send(chat_id, "Дашборд: /dashboard?key=… (адрес и ключ в настройках)")
    if low.startswith(("отмена", "undo")):
        return undo_last(chat_id)
    if low.startswith(("разобрать", "разбор")):
        return review_next(chat_id)
    if low.startswith("долг"):
        return set_card_debt(chat_id, text)

    op = parsing.parse(text)
    if not op or not op.get("amount"):
        return send(chat_id, "Не понял сумму. Например: «кофе 350» или «+50000 комиссия».")
    if not op.get("category"):
        with db.conn() as c:
            c.execute("INSERT OR REPLACE INTO pending(chat_id,payload) VALUES(?,?)",
                      (chat_id, json.dumps(op, ensure_ascii=False)))
        cats = reports.categories_for(op["type"])
        kb = [[{"text": c_, "callback_data": f"cat|{c_}"}] for c_ in cats[:12]]
        return send(chat_id, f"{money(op['amount'], op['currency'])} — какая категория?", kb)
    return save_and_confirm(chat_id, op)

def save_and_confirm(chat_id, op):
    tid = db.add_txn(op.get("date") or dt.date.today().isoformat(), op["type"], op["amount"],
                     op.get("currency", "RUB"), op["category"], note=op.get("note", ""))
    if op.get("note"):
        db.learn_rule(op["note"].split()[0], op["category"])
    sign = {"expense": "−", "income": "+", "transfer": "→"}[op["type"]]
    left = reports.category_left(op["category"])
    tail = f"\nОсталось в лимите: {money(left)}" if left is not None else ""
    return send(chat_id, f"{sign} {money(op['amount'], op.get('currency','RUB'))} · "
                         f"{op['category']}{tail}\n<code>#{tid}</code>")

def handle_callback(cb):
    chat_id = cb["message"]["chat"]["id"]
    data = cb.get("data", "")
    call("answerCallbackQuery", callback_query_id=cb["id"])
    if data.startswith("set|"):
        _, tid, category = data.split("|", 2)
        with db.conn() as c:
            row = c.execute("SELECT note FROM txns WHERE id=?", (tid,)).fetchone()
            kind = c.execute("SELECT kind FROM categories WHERE name=?", (category,)).fetchone()
            c.execute("UPDATE txns SET category=?, type=COALESCE(?,type) WHERE id=?",
                      (category, kind["kind"] if kind else None, tid))
        if row and row["note"]:
            # запоминаем магазин, чтобы в следующий раз категория подставилась сама
            words = [w for w in row["note"].split() if len(w) > 3]
            if words:
                db.learn_rule(words[-1], category)
        send(chat_id, f"Записал: {category}")
        return review_next(chat_id)
    if data.startswith("skip|"):
        _, tid = data.split("|", 1)
        with db.conn() as c:
            c.execute("UPDATE txns SET category='Непредвиденное' WHERE id=?", (tid,))
        return review_next(chat_id)
    if data.startswith("cat|"):
        category = data.split("|", 1)[1]
        with db.conn() as c:
            row = c.execute("SELECT payload FROM pending WHERE chat_id=?", (chat_id,)).fetchone()
            c.execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))
        if not row:
            return send(chat_id, "Операция уже не актуальна.")
        op = json.loads(row["payload"])
        op["category"] = category
        with db.conn() as c:
            k = c.execute("SELECT kind FROM categories WHERE name=?", (category,)).fetchone()
        if k:
            op["type"] = k["kind"]
        return save_and_confirm(chat_id, op)

def set_card_debt(chat_id, text):
    """«долг 45000» или «долг Озон 45000» — записывает задолженность по карте."""
    import datetime as _dt
    nums = [w.replace(" ", "") for w in re.findall(r"[\d][\d \u00a0]*(?:[.,]\d+)?", text)]
    if not nums:
        return send(chat_id, "Напиши сумму: «долг 45000» или «долг Озон 45000».")
    amount = abs(float(nums[-1].replace(",", ".")))
    name_part = re.sub(r"^долг", "", text, flags=re.I)
    name_part = re.sub(r"[\d][\d \u00a0]*(?:[.,]\d+)?", "", name_part).strip()
    with db.conn() as c:
        cards = c.execute("SELECT id,name FROM accounts WHERE kind='card' AND active=1").fetchall()
    if name_part:
        cards = [c_ for c_ in cards if name_part.lower() in c_["name"].lower()] or cards
    if not cards:
        return send(chat_id, "Карт пока нет. Пришли выписку по карте, и счёт появится сам.")
    if len(cards) > 1:
        names = "\n".join("· " + c_["name"] for c_ in cards)
        return send(chat_id, f"Уточни карту, например «долг Озон {amount:.0f}»:\n{names}")
    card = cards[0]
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO balances(account_id,date,balance) VALUES(?,?,?)",
                  (card["id"], _dt.date.today().isoformat(), -amount))
    np_ = reports.net_position()
    return send(chat_id, f"{card['name']}: задолженность {money(amount)}\n"
                         f"Чистая позиция: {money(np_['net'])}")

def review_next(chat_id):
    """Показывает операцию без категории и кнопки для выбора."""
    items = reports.unsorted_txns(1)
    if not items:
        return send(chat_id, "Все операции разобраны.")
    t = items[0]
    left = reports.unsorted_count()
    cats = reports.categories_for("expense") + reports.categories_for("transfer")
    kb = [[{"text": c_, "callback_data": f"set|{t['id']}|{c_}"}] for c_ in cats]
    kb.append([{"text": "Пропустить", "callback_data": f"skip|{t['id']}"}])
    return send(chat_id, f"{t['date']} · {money(t['amount_rub'])}\n<code>{t['note'][:80]}</code>\n"
                         f"Осталось разобрать: {left}", kb)

def undo_last(chat_id):
    with db.conn() as c:
        row = c.execute("SELECT id,amount,currency,category FROM txns WHERE source='bot' ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return send(chat_id, "Нечего отменять.")
        c.execute("DELETE FROM txns WHERE id=?", (row["id"],))
    return send(chat_id, f"Удалил: {money(row['amount'], row['currency'])} · {row['category']}")

def handle_document(chat_id, doc):
    import statements
    name = doc.get("file_name", "file")
    send(chat_id, f"Загружаю {name}…")
    content, _ = get_file(doc["file_id"])
    try:
        added, skipped, report = statements.import_bytes(content, name)
    except Exception as e:
        return send(chat_id, f"Не смог разобрать файл: {e}")
    return send(chat_id, f"Загружено операций: {added}, пропущено дублей: {skipped}\n{report}")
