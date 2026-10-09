"""Flask-приложение: приём вебхука телеграма и дашборд."""
from flask import Flask, request, jsonify, render_template, abort
import config, db, bot, reports

app = Flask(__name__)
db.init()


def check_key():
    if request.args.get("key") != config.DASHBOARD_KEY:
        abort(403)


@app.post("/tg/<secret>")
def telegram(secret):
    if secret != config.WEBHOOK_SECRET:
        abort(403)
    try:
        bot.handle_update(request.get_json(force=True, silent=True) or {})
    except Exception as e:          # бот не должен падать на одном кривом сообщении
        print("handler error:", e)
    return "ok"


@app.get("/set-webhook")
def set_webhook():
    """Открыть один раз после деплоя, чтобы телеграм знал адрес бота."""
    check_key()
    url = request.url_root.rstrip("/") + f"/tg/{config.WEBHOOK_SECRET}"
    return jsonify({"url": url, "telegram": bot.call("setWebhook", url=url,
                                                     drop_pending_updates=True)})


@app.get("/api/month")
def api_month():
    """Данные месяца для Google Таблицы: суммы по дням и категориям."""
    check_key()
    return jsonify(reports.month_matrix(request.args.get("month")))


@app.get("/api/data")
def api_data():
    check_key()
    return jsonify(dashboard_data())


@app.get("/dashboard")
def dashboard():
    check_key()
    return render_template("dashboard.html", data=dashboard_data(), key=config.DASHBOARD_KEY)


@app.get("/")
def index():
    return "Бюджет работает. Дашборд: /dashboard?key=…"


def dashboard_data():
    md = reports.month_data()
    np_ = reports.net_position()
    return {
        "month": md["month"],
        "salary": md["salary"],
        "salary_fact": md["salary_fact"],
        "salary_limit": md["salary_limit"],
        "ip": md["ip"],
        "ip_income": md["ip_income"],
        "income": md["income"],
        "total_income": md["total_income"],
        "net": np_,
        "accounts": reports.balances(),
        "relocation": reports.relocation(),
        "split": reports.ip_split_plan(md["ip_income"]),
        "trend": reports.trend(12),
        "unsorted": reports.unsorted_count(),
        "recent": recent(),
        "rates": {c: db.rate(c) for c in ("ILS", "USD", "USDT")},
    }


def recent(n=12):
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT date, type, amount_rub, currency, amount, category, note
               FROM txns WHERE category NOT IN ('Перевод между счетами')
               ORDER BY date DESC, id DESC LIMIT ?""", (n,))]


if __name__ == "__main__":
    app.run(debug=True, port=5000)
