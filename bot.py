import os
import random
import sqlite3
import logging
import threading
import ast
from flask import Flask
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)


# =========================================================
# SETTINGS
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required.")

try:
    ADMIN_ID = int(ADMIN_ID) if ADMIN_ID else None
except ValueError:
    raise RuntimeError("ADMIN_ID must be a Telegram numeric user ID.")

DB_FILE = os.getenv("DB_FILE", "bingo.db")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

conn = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

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


# =========================================================
# USER
# =========================================================

def ensure_user(user):

    db_execute("""
        INSERT INTO users(
            user_id,
            username,
            first_name,
            points,
            created_at
        )
        VALUES (?, ?, ?, 0, ?)

        ON CONFLICT(user_id)
        DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (
        user.id,
        user.username or "",
        user.first_name or "",
        datetime.utcnow().isoformat(),
    ))


def get_user_name(user_id):

    rows = db_execute(
        """
        SELECT username, first_name
        FROM users
        WHERE user_id=?
        """,
        (user_id,),
        fetch=True,
    )

    if not rows:
        return f"Player {user_id}"

    username = rows[0]["username"]
    first_name = rows[0]["first_name"]

    if first_name and username:
        return f"{first_name} (@{username})"

    if first_name:
        return first_name

    if username:
        return f"@{username}"

    return f"Player {user_id}"


# =========================================================
# GAME
# =========================================================

def active_game():

    rows = db_execute(
        """
        SELECT *
        FROM games
        WHERE active=1
        ORDER BY id DESC
        LIMIT 1
        """,
        fetch=True,
    )

    return rows[0] if rows else None


def get_card(game_id, user_id):

    rows = db_execute(
        """
        SELECT *
        FROM cards
        WHERE game_id=? AND user_id=?
        """,
        (game_id, user_id),
        fetch=True,
    )

    return rows[0] if rows else None


# =========================================================
# BINGO CARD
# =========================================================

def make_card():

    cols = [

        random.sample(
            range(1, 16),
            5
        ),

        random.sample(
            range(16, 31),
            5
        ),

        random.sample(
            range(31, 46),
            5
        ),

        random.sample(
            range(46, 61),
            5
        ),

        random.sample(
            range(61, 76),
            5
        ),
    ]

    grid = [
        [
            cols[c][r]
            for c in range(5)
        ]
        for r in range(5)
    ]

    # FREE center
    grid[2][2] = 0

    return grid


def parse_grid(value):

    try:
        return ast.literal_eval(value)
    except Exception:
        return []


def parse_numbers(value):

    if not value:
        return set()

    return {
        int(x)
        for x in value.split(",")
        if x.strip()
    }


def numbers_text(numbers):

    return ",".join(
        str(x)
        for x in sorted(numbers)
    )


# =========================================================
# BINGO CHECK
# =========================================================

def is_bingo(grid, called):

    # Rows
    for r in range(5):

        if all(
            grid[r][c] == 0
            or grid[r][c] in called
            for c in range(5)
        ):
            return True

    # Columns
    for c in range(5):

        if all(
            grid[r][c] == 0
            or grid[r][c] in called
            for r in range(5)
        ):
            return True

    # Main diagonal
    if all(
        grid[i][i] == 0
        or grid[i][i] in called
        for i in range(5)
    ):
        return True

    # Other diagonal
    if all(
        grid[i][4 - i] == 0
        or grid[i][4 - i] in called
        for i in range(5)
    ):
        return True

    return False


# =========================================================
# CARD DISPLAY
# =========================================================

def card_text(grid, marked):

    lines = [
        "🎫 *Your Bingo Card*",
        "```",
        " B   I   N   G   O"
    ]

    for row in grid:

        cells = []

        for n in row:

            if n == 0:

                cells.append(" ★ ")

            elif n in marked:

                cells.append(
                    f"[{n:2}]"
                )

            else:

                cells.append(
                    f" {n:2} "
                )

        lines.append(
            " ".join(cells)
        )

    lines.append("```")

    lines.append(
        "Numbers in `[ ]` have been called. ★ = FREE"
    )

    return "\n".join(lines)


# =========================================================
# ADMIN CHECK
# =========================================================

def admin_only(update):

    return (
        ADMIN_ID is not None
        and update.effective_user
        and update.effective_user.id == ADMIN_ID
    )


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    ensure_user(
        update.effective_user
    )

    await update.message.reply_text(
        "🎉 Welcome to *Ethio Bingo*!\n\n"
        "Use /join to enter the current game.\n"
        "Use /card to view your card.\n"
        "Use /points to see your points.\n"
        "Use /leaderboard to see the top players.\n"
        "Use /help to see all commands.",
        parse_mode="Markdown",
    )


# =========================================================
# /JOIN
# =========================================================

async def join(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    ensure_user(
        update.effective_user
    )

    game = active_game()

    if not game:

        await update.message.reply_text(
            "⏳ No active game.\n"
            "Ask the admin to use /newgame."
        )

        return

    existing = get_card(
        game["id"],
        update.effective_user.id
    )

    if existing:

        await update.message.reply_text(
            "✅ You are already in this game.\n"
            "Use /card to view your card."
        )

        return

    grid = make_card()

    db_execute(
        """
        INSERT INTO cards(
            game_id,
            user_id,
            numbers,
            marked
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            game["id"],
            update.effective_user.id,
            repr(grid),
            "",
        ),
    )

    await update.message.reply_text(
        "🎫 You joined the game!\n\n"
        + card_text(
            grid,
            set()
        ),
        parse_mode="Markdown",
    )


# =========================================================
# /CARD
# =========================================================

async def card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    ensure_user(
        update.effective_user
    )

    game = active_game()

    if not game:

        await update.message.reply_text(
            "⏳ There is no active game."
        )

        return

    c = get_card(
        game["id"],
        update.effective_user.id
    )

    if not c:

        await update.message.reply_text(
            "You are not in the game.\n"
            "Use /join first."
        )

        return

    grid = parse_grid(
        c["numbers"]
    )

    marked = parse_numbers(
        c["marked"]
    )

    await update.message.reply_text(
        card_text(
            grid,
            marked
        ),
        parse_mode="Markdown",
    )


# =========================================================
# /POINTS
# =========================================================

async def points(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    ensure_user(
        update.effective_user
    )

    rows = db_execute(
        """
        SELECT points
        FROM users
        WHERE user_id=?
        """,
        (
            update.effective_user.id,
        ),
        fetch=True,
    )

    value = (
        rows[0]["points"]
        if rows
        else 0
    )

    await update.message.reply_text(
        f"💰 Your points: *{value}*",
        parse_mode="Markdown",
    )


# =========================================================
# /LEADERBOARD
# =========================================================
async def leaderboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    try:
        rows = db_execute(
            """
            SELECT
                user_id,
                username,
                first_name,
                points
            FROM users
            WHERE points > 0
            ORDER BY points DESC
            LIMIT 10
            """,
            fetch=True,
        )

        if not rows:
            await update.message.reply_text(
                "🏆 Ethio Bingo Leaderboard\n\n"
                "No players have points yet."
            )
            return

        text = "🏆 Ethio Bingo Leaderboard\n\n"

        medals = ["🥇", "🥈", "🥉"]

        for i, row in enumerate(rows):
            name = row["first_name"] or row["username"] or "Player"

            if i < 3:
                medal = medals[i]
            else:
                medal = f"{i + 1}."

            text += (
                f"{medal} {name} — "
                f"💰 {row['points']} points\n"
            )

        text += "\n🎯 Top 10 players"

        await update.message.reply_text(text)

    except Exception:
        logger.exception("Error in /leaderboard")

        await update.message.reply_text(
            "❌ Could not get leaderboard."
        )
