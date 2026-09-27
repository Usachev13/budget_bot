"""Загрузка истории из твоей таблицы «Бюджет 2026» (xlsx).
Месячные листы: колонки G..Q — даты и категории, блок A3:B10 — доходы.
Старые названия категорий переводим в новые."""
import sys, datetime as dt
import openpyxl
import db

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

MAP = {
    "продукты": "Продукты", "тпс": "Продукты",
    "еда вне дома": "Кафе и доставка",
    "красота": "Красота",
    "развлечения": "Развлечения", "развлечения/подарки": "Развлечения",
    "развлечения/подарки/покупки": "Одежда и покупки",
    "здоровье": "Здоровье и спорт", "здоровье/спорт": "Здоровье и спорт", "аптека": "Здоровье и спорт",
    "дети": "Дети и алименты",
    "дом": "Жильё, ЖКХ, связь", "дом/подписки": "Жильё, ЖКХ, связь",
    "транспорт": "Транспорт", "транспорт/доставки": "Транспорт",
    "кредиты": "Погашение карты", "кредиты/штрафы": "Погашение карты",
    "кредиты/штрафы/налоги": "Погашение карты",
}
INCOME_MAP = {
    "зарплата слк": "Зарплата", "зарплата мано": "Зарплата", "зарплата": "Зарплата",
    "ип": "Комиссии ИП", "ринат": "Комиссии ИП", "ринат — чистая комиссия по 17.09": "Комиссии ИП",
    "вклад": "Проценты по счетам", "проценты": "Проценты по счетам",
    "налоговый вычет": "Прочие доходы", "авито": "Прочие доходы", "другое": "Прочие доходы",
    "подарок": "Прочие доходы", "перевели": "Прочие доходы", "прошлый период": None,
    "разбег": None, "компенсация балов": "Прочие доходы", "командировочные": "Прочие доходы",
    "харламов": "Прочие доходы", "прочие доходы ип и проценты": "Комиссии ИП",
}

def run(path, year=2026, skip_months=()):
    """skip_months — месяцы вида '2026-09', уже загруженные из банковских выписок."""
    wb = openpyxl.load_workbook(path, data_only=True)
    added = 0
    for mi, mname in enumerate(MONTHS, start=1):
        if mname not in wb.sheetnames:
            continue
        if f"{year}-{mi:02d}" in skip_months:
            continue
        ws = wb[mname]
        # доходы
        for r in range(3, 11):
            name = ws.cell(r, 1).value
            val = ws.cell(r, 2).value
            if not name or not isinstance(val, (int, float)) or not val:
                continue
            cat = INCOME_MAP.get(str(name).strip().lower(), "Прочие доходы")
            if cat is None:
                continue
            date = dt.date(year, mi, 5).isoformat()
            if db.add_txn(date, "income", val, "RUB", cat, note=f"{name} ({mname})",
                          source="sheet", ext_id=f"sheet-inc-{mname}-{r}"):
                added += 1
        # расходы по дням
        headers = {}
        for col in range(8, 20):
            h = ws.cell(1, col).value
            if h and str(h).strip().lower() not in ("траты", "бюджет на день", "сальдо", "описание"):
                headers[col] = MAP.get(str(h).strip().lower())
        for row in range(2, 40):
            d = ws.cell(row, 7).value
            if not isinstance(d, dt.datetime):
                continue
            if d.date() > dt.date.today():       # шаблоны будущих месяцев не грузим
                continue
            for col, cat in headers.items():
                v = ws.cell(row, col).value
                if not isinstance(v, (int, float)) or abs(v or 0) < 10:
                    continue
                if not cat:
                    cat = "Непредвиденное"
                type_ = "transfer" if cat == "Погашение карты" else "expense"
                if db.add_txn(d.date().isoformat(), type_, v, "RUB", cat,
                              note=f"{mname}, лист бюджета", source="sheet",
                              ext_id=f"sheet-{mname}-{row}-{col}"):
                    added += 1
    return added

if __name__ == "__main__":
    db.init()
    print("Загружено операций:", run(sys.argv[1]))
