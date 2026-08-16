import os
import random
import sqlite3
import logging
import threading
from flask import Flask
from datetime import datetime


from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required.")

try:
    ADMIN_ID = int(ADMIN_ID) if ADMIN_ID else None
except ValueError:
    raise RuntimeError("ADMIN_ID must be a Telegram numeric user ID.")

DB_FILE = os.getenv("DB_FILE", "bingo.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

conn = sqlite3.connect(DB_FILE, check_same_thread=False)
conn.row_factory = sqlite3.Row

def db_execute(sql, params=(), fetch=False, many=False):
    cur = conn.cursor()
    if many:
        cur.executemany(sql, params)
    else:
        cur.execute(sql, params)
    conn.commit()
    if fetch:
        return cur.fetchall()
    return cur.lastrowid

def init_db():
    db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            points INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    db_execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            active INTEGER NOT NULL DEFAULT 1,
            called_numbers TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    db_execute("""
        CREATE TABLE IF NOT EXISTS cards (
            game_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            numbers TEXT NOT NULL,
            marked TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (game_id, user_id)
        )
    """)

def ensure_user(user):
    db_execute("""
        INSERT INTO users(user_id, username, first_name, points, created_at)
        VALUES (?, ?, ?, 0, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (
        user.id,
        user.username or "",
        user.first_name or "",
        datetime.utcnow().isoformat(),
    ))

def active_game():
    rows = db_execute(
        "SELECT * FROM games WHERE active=1 ORDER BY id DESC LIMIT 1",
        fetch=True,
    )
    return rows[0] if rows else None

def get_card(game_id, user_id):
    rows = db_execute(
        "SELECT * FROM cards WHERE game_id=? AND user_id=?",
        (game_id, user_id),
        fetch=True,
    )
    return rows[0] if rows else None

def make_card():
    # Standard 5x5 bingo card: B(1-15), I(16-30), N(31-45), G(46-60), O(61-75)
    cols = [
        random.sample(range(1, 16), 5),
        random.sample(range(16, 31), 5),
        random.sample(range(31, 46), 5),
        random.sample(range(46, 61), 5),
        random.sample(range(61, 76), 5),
    ]
    grid = [[cols[c][r] for c in range(5)] for r in range(5)]
    grid[2][2] = 0  # FREE space
    return grid

def flatten(grid):
    return [n for row in grid for n in row]

def parse_numbers(value):
    if not value:
        return set()
    return {int(x) for x in value.split(",") if x.strip()}

def numbers_text(numbers):
    return ",".join(str(x) for x in sorted(numbers))

def is_bingo(grid, called):
    for r in range(5):
        if all(grid[r][c] == 0 or grid[r][c] in called for c in range(5)):
            return True
    for c in range(5):
        if all(grid[r][c] == 0 or grid[r][c] in called for r in range(5)):
            return True
    if all(grid[i][i] == 0 or grid[i][i] in called for i in range(5)):
        return True
    if all(grid[i][4-i] == 0 or grid[i][4-i] in called for i in range(5)):
        return True
    return False

def card_text(grid, marked):
    lines = ["🎫 *Your Bingo Card*", "```"]
    lines.append(" B   I   N   G   O")
    for row in grid:
        cells = []
        for n in row:
            if n == 0:
                cells.append(" ★ ")
            elif n in marked:
                cells.append(f"[{n:2}]")
            else:
                cells.append(f" {n:2} ")
        lines.append(" ".join(cells))
    lines.append("```")
    lines.append("Numbers in `[ ]` have been called. ★ = FREE")
    return "\n".join(lines)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_text(
        "🎉 Welcome to *Ethio Bingo*!\n\n"
        "Use /join to enter the current game.\n"
        "Use /card to view your card.\n"
        "Use /points to see your points.",
        parse_mode="Markdown",
    )

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    game = active_game()
    if not game:
        await update.message.reply_text("⏳ No active game. Ask the admin to use /newgame.")
        return

    existing = get_card(game["id"], update.effective_user.id)
    if existing:
        await update.message.reply_text("✅ You are already in this game. Use /card.")
        return

    grid = make_card()
    db_execute(
        "INSERT INTO cards(game_id,user_id,numbers,marked) VALUES(?,?,?,?)",
        (game["id"], update.effective_user.id, repr(grid), ""),
    )
    await update.message.reply_text(
        "🎫 You joined the game!\n\n" + card_text(grid, set()),
        parse_mode="Markdown",
    )

async def card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    game = active_game()
    if not game:
        await update.message.reply_text("⏳ There is no active game.")
        return
    c = get_card(game["id"], update.effective_user.id)
    if not c:
        await update.message.reply_text("You are not in the game. Use /join first.")
        return
    grid = eval(c["numbers"], {"__builtins__": {}}, {})
    marked = parse_numbers(c["marked"])
    await update.message.reply_text(card_text(grid, marked), parse_mode="Markdown")

async def points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    rows = db_execute(
        "SELECT points FROM users WHERE user_id=?",
        (update.effective_user.id,),
        fetch=True,
    )
    value = rows[0]["points"] if rows else 0
    await update.message.reply_text(f"💰 Your points: *{value}*", parse_mode="Markdown")

def admin_only(update):
    return ADMIN_ID is not None and update.effective_user and update.effective_user.id == ADMIN_ID

async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("⛔ Admin only.")
        return

    db_execute("UPDATE games SET active=0 WHERE active=1")
    game_id = db_execute(
        "INSERT INTO games(active,called_numbers,created_at) VALUES(1,'',?)",
        (datetime.utcnow().isoformat(),),
    )
    await update.message.reply_text(
        f"🎲 New Bingo game #{game_id} created!\nPlayers can now use /join."
    )

async def call_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("⛔ Admin only.")
        return

    game = active_game()
    if not game:
        await update.message.reply_text("⏳ Create a game first with /newgame.")
        return

    called = parse_numbers(game["called_numbers"])
    remaining = [n for n in range(1, 76) if n not in called]
    if not remaining:
        await update.message.reply_text("🏁 All 75 numbers have been called.")
        return

    number = random.choice(remaining)
    called.add(number)
    db_execute(
        "UPDATE games SET called_numbers=? WHERE id=?",
        (numbers_text(called), game["id"]),
    )

    winner = None
    cards = db_execute(
        "SELECT * FROM cards WHERE game_id=?",
        (game["id"],),
        fetch=True,
    )

    for c in cards:
        grid = eval(c["numbers"], {"__builtins__": {}}, {})
        marked = parse_numbers(c["marked"])
        marked.add(number)
        db_execute(
            "UPDATE cards SET marked=? WHERE game_id=? AND user_id=?",
            (numbers_text(marked), game["id"], c["user_id"]),
        )
        if is_bingo(grid, marked):
            winner = c["user_id"]
            break

    if winner:
        db_execute("UPDATE users SET points=points+100 WHERE user_id=?", (winner,))
        db_execute("UPDATE games SET active=0 WHERE id=?", (game["id"],))
        await update.message.reply_text(
            f"🔔 *BINGO NUMBER: {number}*\n\n"
            f"🏆 We have a winner!\n"
            f"👤 Player ID: `{winner}`\n"
            f"💰 +100 points\n\n"
            "🎉 Game finished. Admin can use /newgame for another game.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"🔔 *BINGO NUMBER: {number}*\n"
            f"📢 Called numbers: {len(called)}/75",
            parse_mode="Markdown",
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎲 *Ethio Bingo Commands*\n\n"
        "/start - Register and welcome\n"
        "/join - Join the active game\n"
        "/card - Show your card\n"
        "/points - Show your points\n\n"
        "Admin:\n"
        "/newgame - Create a new game\n"
        "/call - Call the next number",
        parse_mode="Markdown",
    )

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("card", card))
    app.add_handler(CommandHandler("points", points))
    app.add_handler(CommandHandler("newgame", newgame))
    app.add_handler(CommandHandler("call", call_number))
    app.add_handler(CommandHandler("help", help_cmd))

    logger.info("Ethio Bingo bot is starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
