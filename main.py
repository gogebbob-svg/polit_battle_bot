import asyncio
import gc
import io
import logging
import os
import random
import re
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from aiohttp import web
from PIL import Image, ImageDraw
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

# ============================================================
# POLYT BATTLE BOT (Render, PostgreSQL & RAM Optimized Edition)
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
BULBA_ID = 5137972241

DEFAULT_ROUND_SECONDS = 5 * 60

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("polyt-battle")

# -------------------- Database (PostgreSQL) --------------------

def get_db_connection():
    db_url = DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    return conn

def init_db():
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS players (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            country TEXT,
            ideology TEXT,
            capital TEXT,
            money BIGINT DEFAULT 10000000,
            population BIGINT DEFAULT 0,
            army BIGINT DEFAULT 0,
            rank TEXT DEFAULT 'player',
            regions INTEGER DEFAULT 0,
            income BIGINT DEFAULT 0,
            expenses BIGINT DEFAULT 0,
            kicked INTEGER DEFAULT 0,
            muted_until TEXT,
            joined_at TEXT
        );

        CREATE TABLE IF NOT EXISTS regions (
            id SERIAL PRIMARY KEY,
            owner_id BIGINT,
            name TEXT NOT NULL,
            population BIGINT NOT NULL,
            cities INTEGER NOT NULL,
            x INTEGER NOT NULL,
            y INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS borders (
            region_a INTEGER NOT NULL,
            region_b INTEGER NOT NULL,
            PRIMARY KEY(region_a, region_b)
        );

        CREATE TABLE IF NOT EXISTS alliances (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            creator_id BIGINT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS alliance_members (
            alliance_id INTEGER NOT NULL,
            user_id BIGINT NOT NULL,
            PRIMARY KEY(alliance_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS wars (
            id SERIAL PRIMARY KEY,
            a_id BIGINT NOT NULL,
            b_id BIGINT NOT NULL,
            active INTEGER DEFAULT 1,
            proposed_peace_by BIGINT,
            UNIQUE(a_id, b_id)
        );

        CREATE TABLE IF NOT EXISTS round_actions (
            round_no INTEGER NOT NULL,
            user_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            target_id BIGINT,
            amount BIGINT DEFAULT 0,
            region_id INTEGER,
            PRIMARY KEY(round_no, user_id)
        );

        CREATE TABLE IF NOT EXISTS stats (
            user_id BIGINT PRIMARY KEY,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            regions_won INTEGER DEFAULT 0,
            regions_lost INTEGER DEFAULT 0,
            kills BIGINT DEFAULT 0,
            casualties BIGINT DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS pending (
            kind TEXT NOT NULL,
            from_id BIGINT NOT NULL,
            to_id BIGINT NOT NULL,
            payload TEXT DEFAULT '',
            PRIMARY KEY(kind, from_id, to_id)
        );
        """)

        defaults = {
            "round_seconds": str(DEFAULT_ROUND_SECONDS),
            "round_no": "1",
            "round_started": datetime.now(timezone.utc).isoformat(),
            "ai_map": "1",
            "game_started": "1",
        }
        for k, v in defaults.items():
            cur.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT (key) DO NOTHING", (k, v))
    conn.close()

def setting(key):
    conn = get_db_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
        row = cur.fetchone()
    conn.close()
    return row["value"] if row else None

def player(uid):
    conn = get_db_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM players WHERE user_id=%s", (uid,))
        row = cur.fetchone()
    conn.close()
    return row

def is_bulba(uid):
    return uid == BULBA_ID

def is_admin(uid):
    p = player(uid)
    return is_bulba(uid) or (p and p["rank"] in ("admin", "bulba"))

def active_players():
    conn = get_db_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM players WHERE kicked=0")
        rows = cur.fetchall()
    conn.close()
    return rows

def fmt_money(n):
    return f"${n:,.0f}".replace(",", " ")

def fmt_num(n):
    return f"{int(n):,}".replace(",", " ")

# -------------------- Map engine --------------------

REGION_NAMES = [
    "Північні землі","Сонячний край","Західні поля","Східні степи",
    "Срібний берег","Чорні гори","Зелений край","Вільні землі",
    "Бурштинова долина","Королівські рівнини","Туманні землі",
    "Червоні скелі","Лісовий край","Долина річок","Золоті поля",
    "Кам'яний край","Блакитний берег","Вітряні землі","Старі землі",
    "Нова долина","Південний край","Озелені рівнини","Високі гори",
    "Морський край","Річковий край","Східний берег","Дикі поля",
    "Смарагдові землі","Білий край","Темна долина",
]

def random_region_name(i):
    base = REGION_NAMES[i % len(REGION_NAMES)]
    suffix = "" if i < len(REGION_NAMES) else f" {i // len(REGION_NAMES)+1}"
    return base + suffix

def generate_map():
    ps = active_players()
    if not ps:
        return None

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM regions")
        cur.execute("DELETE FROM borders")
        idx = 0
        coords = []
        for p in ps:
            count = random.randint(10, 30)
            for _ in range(count):
                pop = random.randint(100_000, 500_000)
                cities = random.randint(1, 4)
                x = idx % 10
                y = idx // 10
                cur.execute("""
                    INSERT INTO regions(owner_id,name,population,cities,x,y)
                    VALUES(%s,%s,%s,%s,%s,%s) RETURNING id
                """, (p["user_id"], random_region_name(idx), pop, cities, x, y))
                rid = cur.fetchone()[0]
                coords.append((rid, x, y))
                idx += 1

        for a, x, y in coords:
            for b, xx, yy in coords:
                if a >= b:
                    continue
                if abs(x-xx) + abs(y-yy) == 1:
                    cur.execute("""
                        INSERT INTO borders(region_a,region_b) VALUES(%s,%s)
                        ON CONFLICT DO NOTHING
                    """, (a, b))

        for p in ps:
            recalc_country(p["user_id"])

    conn.close()
    return render_map_bytes()

def render_map_bytes():
    conn = get_db_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT r.*, p.country, p.username
            FROM regions r LEFT JOIN players p ON p.user_id=r.owner_id
            ORDER BY r.id
        """)
        rows = cur.fetchall()
        cur.execute("SELECT user_id FROM players ORDER BY user_id")
        users = cur.fetchall()
    conn.close()

    if not rows:
        return None

    max_x = max(r["x"] for r in rows)
    max_y = max(r["y"] for r in rows)
    cell_w, cell_h = 150, 105
    margin = 30
    img = Image.new("RGB", (
        (max_x+1)*cell_w + margin*2,
        (max_y+1)*cell_h + margin*2 + 50
    ), "white")
    draw = ImageDraw.Draw(img)

    palette = {}
    colors = [
        "#4E79A7","#F28E2B","#E15759","#76B7B2","#59A14F",
        "#EDC949","#AF7AA1","#FF9DA7","#9C755F","#BAB0AC",
        "#2F4B7C","#665191","#A05195","#D45087","#F95D6A",
        "#FF7C43","#FFA600","#1B9E77","#D95F02","#7570B3",
    ]
    for i, p in enumerate(users):
        palette[p["user_id"]] = colors[i % len(colors)]

    for r in rows:
        x1 = margin + r["x"]*cell_w
        y1 = margin + r["y"]*cell_h
        x2 = x1 + cell_w - 4
        y2 = y1 + cell_h - 4
        fill = palette.get(r["owner_id"], "#CCCCCC")
        draw.rectangle((x1,y1,x2,y2), fill=fill, outline="black", width=2)
        label = f'#{r["id"]} {r["name"][:18]}'
        draw.text((x1+5,y1+5), label, fill="black")
        draw.text((x1+5,y1+27), f'{r["cities"]} міст • {fmt_num(r["population"])}', fill="black")
        draw.text((x1+5,y1+49), (r["country"] or "Немає")[:22], fill="black")

    draw.text((margin, (max_y+1)*cell_h + margin+10),
              f"POLYT BATTLE • Раунд {setting('round_no')} • AI MAP ENGINE",
              fill="black")
    
    bio = io.BytesIO()
    bio.name = 'map.png'
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio

# -------------------- Economy --------------------

def recalc_country(uid):
    p = player(uid)
    if not p:
        return
    
    conn = get_db_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT COUNT(*) regions, COALESCE(SUM(population),0) population,
                   COALESCE(SUM(cities),0) cities
            FROM regions WHERE owner_id=%s
        """, (uid,))
        rr = cur.fetchone()

        regions = rr["regions"]
        population = rr["population"]
        cities = rr["cities"]

        ideology = p["ideology"] or ""
        city_income = cities * 200_000
        if ideology == "Демократія":
            city_income = int(city_income * 1.20)

        tax_income = (population // 100_000) * 50_000
        income = city_income + tax_income

        army_cost = p["army"] * 10
        if ideology == "Демократія":
            army_cost = int(army_cost * 1.10)
        if ideology == "Капіталізм":
            army_cost = int(army_cost * 1.15)

        expenses = army_cost
        net = income - expenses

        cur.execute("""
            UPDATE players SET regions=%s, population=%s, income=%s, expenses=%s
            WHERE user_id=%s
        """, (regions, population, income, expenses, uid))
    conn.close()
    return income, expenses, net

# -------------------- Handlers --------------------

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Реєстрація", callback_data="register")],
    ])

def ideology_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Демократія", callback_data="ideo:Демократія"),
         InlineKeyboardButton("Капіталізм", callback_data="ideo:Капіталізм")],
        [InlineKeyboardButton("Комунізм", callback_data="ideo:Комунізм"),
         InlineKeyboardButton("Фашизм", callback_data="ideo:Фашизм")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = player(uid)
    if not p:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO players(user_id,username,first_name,rank,joined_at)
                VALUES(%s,%s,%s,%s,%s)
            """, (
                uid, update.effective_user.username or "",
                update.effective_user.first_name or "",
                "bulba" if is_bulba(uid) else "player",
                datetime.now(timezone.utc).isoformat()
            ))
            cur.execute("INSERT INTO stats(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (uid,))
        conn.close()
        p = player(uid)

    if p["country"]:
        await update.message.reply_text("Твоя країна вже створена.\n\nНапиши /menu, щоб відкрити меню.")
        return

    await update.message.reply_text("Я Політ Батл бот для реєстрації.\nНатисніть кнопку «Реєстрація».", reply_markup=main_menu())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = player(uid)
    if not p or not p["country"]:
        await update.message.reply_text("Спочатку зареєструйся через /start.")
        return

    text = country_text(uid)
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🗺️ Мапа", callback_data="showmap")],
        [InlineKeyboardButton("🏆 Топ", callback_data="top")],
    ]))

def country_text(uid):
    recalc_country(uid)
    p = player(uid)
    net = p["income"] - p["expenses"]
    return (
        "🏛️ Моя країна\n\n"
        f"Назва: {p['country']}\n"
        f"Назва столиці: {p['capital']}\n"
        f"Ідеологія: {p['ideology']}\n"
        f"Регіони: {p['regions']}\n"
        f"Гроші: {fmt_money(p['money'])}\n"
        f"Кількість населення: {fmt_num(p['population'])}\n"
        f"Кількість армії: {fmt_num(p['army'])}\n"
        f"Заробіток: {fmt_money(p['income'])}\n"
        f"Витрати: {fmt_money(p['expenses'])}\n"
        f"Кінцевий заробіток: {fmt_money(net)}\n"
        f"Ранг: {rank_name(p['rank'])}"
    )

def rank_name(r):
    return {"player":"Гравець","admin":"Адміністратор","bulba":"Бульба"}.get(r, "Гравець")

IDEO_INFO = {
    "Демократія": "➕ +20% до доходу від міст\n➖ Витрати на армію +10%",
    "Капіталізм": "➕ Початковий капітал $15 млн\n➖ Витрати на армію +15%",
    "Комунізм": "➕ Мобілізація безкоштовна і дає +15% населення\n➖ Дохід від міст фіксований",
    "Фашизм": "➕ Атака ×1.3\n➖ Населення щораунду випадково зменшується на 0.1–0.3%",
}

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "register":
        context.user_data["reg_step"] = "country"
        await q.message.reply_text("Напишіть назву вашої країни або натисніть «Створити випадкову».",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎲 Створити випадкову", callback_data="random_country")]]))
        return

    if data == "random_country":
        names = ["Астерія","Велорія","Драконія","Норделія","Соларія","Елварія","Терранія","Аркадія"]
        context.user_data["country"] = random.choice(names) + str(random.randint(1,999))
        context.user_data["reg_step"] = "ideology"
        await q.message.reply_text("Вибери ідеологію:", reply_markup=ideology_menu())
        return

    if data.startswith("ideo:"):
        ideology = data.split(":",1)[1]
        context.user_data["ideology"] = ideology
        await q.message.reply_text(f"{ideology}\n\n{IDEO_INFO[ideology]}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Вибір ідеології", callback_data="chooseideo")],
                [InlineKeyboardButton("⬅️ Назад до вибору", callback_data="backideo")],
            ]))
        return

    if data == "backideo":
        await q.message.reply_text("Вибери ідеологію:", reply_markup=ideology_menu())
        return

    if data == "chooseideo":
        context.user_data["reg_step"] = "capital"
        await q.message.reply_text("Зроби назву столиці.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✍️ Написати назву", callback_data="writecapital"),
                 InlineKeyboardButton("🎲 Випадкова назва", callback_data="randomcapital")]
            ]))
        return

    if data == "writecapital":
        context.user_data["reg_step"] = "capital_text"
        await q.message.reply_text("Напишіть назву столиці.")
        return

    if data == "randomcapital":
        caps = ["Аурелія","Варден","Еліс","Новар","Селест","Арден","Ліар","Терран"]
        context.user_data["capital"] = random.choice(caps) + str(random.randint(1,99))
        await finish_registration(q.message, uid, context)
        return

    if data == "showmap":
        await send_map(q.message)
        return

    if data == "top":
        await send_top(q.message)
        return

async def finish_registration(message, uid, context):
    country = context.user_data["country"]
    ideology = context.user_data["ideology"]
    capital = context.user_data["capital"]
    start_money = 15_000_000 if ideology == "Капіталізм" else 10_000_000

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE players SET country=%s, ideology=%s, capital=%s, money=%s
            WHERE user_id=%s
        """, (country, ideology, capital, start_money, uid))
        cur.execute("SELECT COUNT(*) FROM regions")
        c = cur.fetchone()[0]
    conn.close()

    if c == 0:
        generate_map()
    else:
        give_regions_to_player(uid, random.randint(10,30))

    p = player(uid)
    army = int(p["population"] * 0.02)
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE players SET army=%s WHERE user_id=%s", (army, uid))
    conn.close()
    
    recalc_country(uid)
    context.user_data.clear()
    await message.reply_text("✅ Реєстрацію завершено!\n\n" + country_text(uid) + "\n\nНапиши /menu, щоб відкрити меню.")

async def send_map(message):
    bio = render_map_bytes()
    if bio:
        try:
            await message.reply_photo(photo=bio, caption=f"🗺️ Карта • Раунд {setting('round_no')}")
        finally:
            bio.close()
            gc.collect()  # Примусово звільняємо RAM від зображення
    else:
        await message.reply_text("🗺️ Карта ще не створена.")

async def send_top(message):
    conn = get_db_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM players WHERE kicked=0 AND country IS NOT NULL ORDER BY money DESC LIMIT 10")
        rows = cur.fetchall()
    conn.close()

    if not rows:
        await message.reply_text("Топ поки порожній.")
        return
    
    text = "🏆 ТОП\n\n💰 За грошима:\n"
    for i,p in enumerate(rows,1):
        text += f"{i}. {p['country']} — {fmt_money(p['money'])}\n"
    await message.reply_text(text)

def give_regions_to_player(uid, n):
    if n <= 0: return 0
    conn = get_db_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT id FROM regions WHERE owner_id!=%s LIMIT %s", (uid, n))
        candidates = cur.fetchall()
        for r in candidates:
            cur.execute("UPDATE regions SET owner_id=%s WHERE id=%s", (uid, r["id"]))
    conn.close()
    recalc_country(uid)
    return len(candidates)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    step = context.user_data.get("reg_step")
    text = update.message.text.strip()

    if step == "country":
        if len(text) < 2 or len(text) > 40:
            await update.message.reply_text("Назва має бути від 2 до 40 символів.")
            return
        context.user_data["country"] = text
        context.user_data["reg_step"] = "ideology"
        await update.message.reply_text("Вибери ідеологію:", reply_markup=ideology_menu())
        return

    if step == "capital_text":
        if len(text) < 2 or len(text) > 40:
            await update.message.reply_text("Назва столиці має бути від 2 до 40 символів.")
            return
        context.user_data["capital"] = text
        await finish_registration(update.message, uid, context)
        return

# -------------------- RAM Clean & Admin --------------------

async def clear_memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ У вас немає прав для цієї команди.")
        return

    collected = gc.collect()
    try:
        import psutil
        process = psutil.Process(os.getpid())
        ram_mb = process.memory_info().rss / 1024 / 1024
        ram_info = f"\n📊 Поточне використання RAM: **{ram_mb:.2f} MB** / 512 MB"
    except ImportError:
        ram_info = ""

    await update.message.reply_text(
        f"🧹 **Оперативну пам'ять (RAM) очищено!**\n"
        f"Зібрано непотрібних об'єктів з пам'яті: `{collected}`{ram_info}\n\n"
        f"ℹ️ *Всі дані гри (база, ходи, регіони) залишилися без змін.*",
        parse_mode="Markdown"
    )

# -------------------- Web Server (Захист від сну) --------------------

async def handle_health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    port = int(os.getenv("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Health check web server running on port {port}")

# -------------------- Main --------------------

async def on_startup(app: Application):
    # Запускаємо вебсервер під час старт-апу бота в правильному event loop
    await start_web_server()

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is missing.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("map", lambda u,c: send_map(u.message)))
    app.add_handler(CommandHandler("top", lambda u,c: send_top(u.message)))
    app.add_handler(CommandHandler("clearmem", clear_memory_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    log.info("Bot started successfully!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
