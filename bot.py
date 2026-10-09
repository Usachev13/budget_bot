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

MENU = [["📊 Итоги", "💳 Счета"], ["📅 День", "🗂 Разобрать"], ["↩️ Отмена", "❓ Помощь"]]

COMMANDS = [
    ("итоги", "лимиты месяца, доходы, чистая позиция"),
    ("счета", "остатки и свободные лимиты по картам"),
    ("день", "траты за сегодня"),
    ("разобрать", "категории для операций из выписок"),
    ("отмена", "удалить последнюю операцию"),
    ("синхрон", "обновить справочники из Google Таблицы"),
    ("дашборд", "ссылка на дашборд"),
    ("помощь", "как пользоваться"),
]


def send(chat_id, text, keyboard=None, menu=False):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    elif menu:
        payload["reply_markup"] = {"keyboard": MENU, "resize_keyboard": True,
                                   "is_persistent": True}
    return call("sendMessage", **payload)


def setup_menu():
    """Список команд в меню телеграма плюс кнопка «Меню»."""
    call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    return call("setChatMenuButton", menu_button={"type": "commands"})


def drop_keyboard(chat_id, message_id):
    """Убирает кнопки у отвеченного вопроса, чтобы по ним нельзя было нажать повторно."""
    call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
         reply_markup={"inline_keyboard": []})

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
    "/синхрон — подтянуть категории и правила из Google Таблицы\n"
    "долг 45000 — записать текущую задолженность по карте\n"
    "поправь 1895 жильё — сменить категорию операции на эту сумму\n"
    "/отмена — удалить последнюю операцию\n"
    "/дашборд — ссылка на дашборд\n\n"
    "<b>Счета</b>\n"
    "Чтобы списать с конкретной карты, добавь её название: «кофе 350 озон».\n"
    "«счёт по умолчанию яндекс» — откуда списывать, если карта не указана.\n"
    "«добавь карту Сбер 100000» — новая кредитка с лимитом.\n"
    "«добавь счёт Альфа» — обычный счёт."
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

BUTTON_WORDS = {"📊 итоги": "итоги", "💳 счета": "счета", "📅 день": "день",
                "🗂 разобрать": "разобрать", "↩️ отмена": "отмена", "❓ помощь": "помощь"}


def handle_text(chat_id, text):
    low = text.lower().lstrip("/").strip()
    low = BUTTON_WORDS.get(low, low)
    if low in ("start", "меню", "menu"):
        setup_menu()
        return send(chat_id, HELP, menu=True)
    if low in ("help", "помощь"):
        return send(chat_id, HELP, menu=True)
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
    if low.startswith(("синхрон", "sync", "таблица")):
        return sync_sheet(chat_id)
    if low.startswith(("разобрать", "разбор")):
        return review_next(chat_id)
    if low.startswith("долг"):
        return set_card_debt(chat_id, text)
    if low.startswith(("поправь", "исправь")):
        return fix_category(chat_id, text)
    if low.startswith(("добавь карту", "добавь счёт", "добавь счет")):
        return add_account(chat_id, text)
    if low.startswith(("счёт по умолчанию", "счет по умолчанию", "по умолчанию")):
        return set_default_account(chat_id, text)

    op = parsing.parse(text)
    if not op or not op.get("amount"):
        return send(chat_id, "Не понял сумму. Например: «кофе 350» или «+50000 комиссия».")
    op["account_id"] = db.find_account(text) or default_account_id()
    if not op.get("category"):
        with db.conn() as c:
            cur = c.execute("INSERT INTO pending(chat_id,payload) VALUES(?,?)",
                            (chat_id, json.dumps(op, ensure_ascii=False)))
            pid = cur.lastrowid
        cats = reports.categories_for(op["type"])
        kb = [[{"text": c_, "callback_data": f"cat|{pid}|{c_}"}] for c_ in cats[:12]]
        return send(chat_id, f"{money(op['amount'], op['currency'])} — какая категория?", kb)
    return save_and_confirm(chat_id, op)


def default_account_id():
    name = db.get_setting("default_account")
    return db.account_by_name(name) if name else None


def account_name(account_id):
    if not account_id:
        return ""
    with db.conn() as c:
        r = c.execute("SELECT name FROM accounts WHERE id=?", (account_id,)).fetchone()
    return r["name"] if r else ""


def add_account(chat_id, text):
    """«добавь карту Сбер 100000» или «добавь счёт Альфа»."""
    is_card = "карт" in text.lower()
    rest = re.sub(r"^добавь\s+(карту|счёт|счет)\s*", "", text.strip(), flags=re.I)
    m = re.search(r"([\d][\d \u00a0]*)\s*$", rest)
    limit = float(m.group(1).replace(" ", "").replace("\u00a0", "")) if m else 0.0
    name = rest[:m.start()].strip() if m else rest.strip()
    if not name:
        return send(chat_id, "Напиши название: «добавь карту Сбер 100000».")
    aid = db.account_by_name(name, create_kind="card" if is_card else "current")
    with db.conn() as c:
        c.execute("UPDATE accounts SET kind=?, credit_limit=? WHERE id=?",
                  ("card" if is_card else "current", limit, aid))
    tail = f", лимит {money(limit)}" if is_card and limit else ""
    return send(chat_id, f"Добавил {'карту' if is_card else 'счёт'} «{name}»{tail}.\n"
                         f"Теперь можно писать «кофе 350 {name.split()[0].lower()}».")


def set_default_account(chat_id, text):
    query = re.sub(r"^(счёт|счет)?\s*по умолчанию\s*", "", text.strip(), flags=re.I).strip()
    aid = db.find_account(query) if query else None
    if not aid:
        with db.conn() as c:
            names = [r["name"] for r in c.execute("SELECT name FROM accounts WHERE active=1")]
        return send(chat_id, "Не нашёл такой счёт. Есть:\n" + "\n".join("· " + n for n in names))
    db.set_setting("default_account", account_name(aid))
    return send(chat_id, f"Списываю по умолчанию с «{account_name(aid)}».")


def save_and_confirm(chat_id, op):
    account_id = op.get("account_id") or default_account_id()
    tid = db.add_txn(op.get("date") or dt.date.today().isoformat(), op["type"], op["amount"],
                     op.get("currency", "RUB"), op["category"], account_id=account_id,
                     note=op.get("note", ""))
    if op.get("note"):
        db.learn_rule(op["note"].split()[0], op["category"])
    sign = {"expense": "−", "income": "+", "transfer": "→"}[op["type"]]
    parts = [f"{sign} {money(op['amount'], op.get('currency', 'RUB'))} · {op['category']}"]
    if account_id:
        parts[0] += f" · {account_name(account_id)}"
    left = reports.category_left(op["category"])
    if left is not None:
        parts.append(f"Осталось в лимите: {money(left)}")
    free = reports.card_available(account_id) if account_id else None
    if free is not None:
        parts.append(f"Свободно по карте: {money(free)}")
    parts.append(f"<code>#{tid}</code>")
    return send(chat_id, "\n".join(parts))


def handle_callback(cb):
    chat_id = cb["message"]["chat"]["id"]
    data = cb.get("data", "")
    call("answerCallbackQuery", callback_query_id=cb["id"])
    if data.startswith("setg|"):
        _, tid, category = data.split("|", 2)
        with db.conn() as c:
            row = c.execute("SELECT note FROM txns WHERE id=?", (tid,)).fetchone()
        key = reports.merchant_key(row["note"] if row else "")
        group = next((g for g in reports.unsorted_groups() if g["key"] == key), None)
        ids = group["ids"] if group else [int(tid)]
        reports.apply_category_to_group(ids, category)
        if key and key != "без описания":
            db.learn_rule(key[:40], category)   # в следующий раз подставится само
        send(chat_id, f"Записал {len(ids)} операц. в «{category}»")
        return review_next(chat_id)
    if data.startswith("skipg|"):
        _, tid = data.split("|", 1)
        with db.conn() as c:
            row = c.execute("SELECT note FROM txns WHERE id=?", (tid,)).fetchone()
        key = reports.merchant_key(row["note"] if row else "")
        group = next((g for g in reports.unsorted_groups() if g["key"] == key), None)
        reports.apply_category_to_group(group["ids"] if group else [int(tid)], "Непредвиденное")
        return review_next(chat_id)
    if data.startswith("cat|"):
        _, pid, category = data.split("|", 2)
        with db.conn() as c:
            row = c.execute("SELECT payload FROM pending WHERE id=?", (pid,)).fetchone()
            c.execute("DELETE FROM pending WHERE id=?", (pid,))
        drop_keyboard(chat_id, cb["message"]["message_id"])
        if not row:
            return send(chat_id, "Эта операция уже записана или отменена.")
        op = json.loads(row["payload"])
        op["category"] = category
        with db.conn() as c:
            k = c.execute("SELECT kind FROM categories WHERE name=?", (category,)).fetchone()
        if k:
            op["type"] = k["kind"]
        return save_and_confirm(chat_id, op)

def sync_sheet(chat_id):
    """Забирает справочники из Google Таблицы."""
    import sheet_sync
    try:
        res = sheet_sync.sync_all()
    except Exception as e:
        return send(chat_id, f"Не смог прочитать таблицу: {e}")
    return send(chat_id,
                f"Из таблицы загружено:\n· категорий — {res['categories']}\n"
                f"· правил — {res['rules']}\n· счетов — {res['accounts']}\n"
                f"Сумма лимитов из зарплаты: {money(res['salary_limit'])}")

def fix_category(chat_id, text):
    """«поправь 1895 жильё» — меняет категорию у операции с такой суммой."""
    m = re.search(r"([\d][\d \u00a0]*(?:[.,]\d+)?)\s+(.+)$", re.sub(r"^(поправь|исправь)", "", text, flags=re.I).strip())
    if not m:
        return send(chat_id, "Формат: «поправь 1895 жильё».")
    amount = float(m.group(1).replace(" ", "").replace("\u00a0", "").replace(",", "."))
    query = m.group(2).strip().lower()
    with db.conn() as c:
        cats = [r["name"] for r in c.execute("SELECT name FROM categories ORDER BY sort")]
    match = [c_ for c_ in cats if c_.lower().startswith(query)] or [c_ for c_ in cats if query in c_.lower()]
    if not match:
        return send(chat_id, "Не нашёл такую категорию. Посмотри список в /итоги.")
    category = match[0]
    with db.conn() as c:
        row = c.execute("""SELECT id, note FROM txns WHERE ROUND(amount_rub,2)=ROUND(?,2)
                           ORDER BY date DESC, id DESC LIMIT 1""", (amount,)).fetchone()
        if not row:
            return send(chat_id, f"Операции на {money(amount)} не нашёл.")
        kind = c.execute("SELECT kind FROM categories WHERE name=?", (category,)).fetchone()
        c.execute("UPDATE txns SET category=?, type=COALESCE(?,type) WHERE id=?",
                  (category, kind["kind"] if kind else None, row["id"]))
    return send(chat_id, f"{money(amount)} → «{category}»\n<code>{(row['note'] or '')[:60]}</code>")

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
    """Показывает самую крупную группу операций без категории и кнопки выбора."""
    groups = reports.unsorted_groups()
    if not groups:
        return send(chat_id, "Все операции разобраны.")
    g = groups[0]
    left = sum(x["count"] for x in groups)
    cats = reports.categories_for("expense") + reports.categories_for("transfer")
    kb = [[{"text": c_, "callback_data": f"setg|{g['ids'][0]}|{c_}"}] for c_ in cats]
    kb.append([{"text": "Пропустить группу", "callback_data": f"skipg|{g['ids'][0]}"}])
    head = (f"<b>{g['count']} операций</b> на {money(g['sum'])}\n<code>{(g['note'] or '')[:80]}</code>"
            if g["count"] > 1 else
            f"{g['date']} · {money(g['sum'])}\n<code>{(g['note'] or '')[:80]}</code>")
    return send(chat_id, f"{head}\nВсего без категории: {left}\nКакая категория?", kb)

def undo_last(chat_id):
    """Удаляет последнюю запись и снимает незавершённые вопросы о категории."""
    with db.conn() as c:
        pend = c.execute("SELECT COUNT(*) n FROM pending WHERE chat_id=?", (chat_id,)).fetchone()["n"]
        c.execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))
        row = c.execute("""SELECT id,amount,currency,category FROM txns
                           WHERE source='bot' ORDER BY id DESC LIMIT 1""").fetchone()
        if not row:
            return send(chat_id, "Нечего отменять." + (f" Снял {pend} незаданный вопрос." if pend else ""))
        c.execute("DELETE FROM txns WHERE id=?", (row["id"],))
    tail = "\nНезавершённые вопросы о категории тоже снял." if pend else ""
    return send(chat_id, f"Удалил: {money(row['amount'], row['currency'])} · {row['category']}{tail}")

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
