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
# CONFIG
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
    check_same_thread=False,
    timeout=30,
)

conn.row_factory = sqlite3.Row

# Prevent SQLite operations from colliding
DB_LOCK = threading.RLock()


def db_execute(sql, params=(), fetch=False, many=False):
    """
    Safe SQLite helper.
    """

    with DB_LOCK:
        cur = conn.cursor()

        try:
            if many:
                cur.executemany(sql, params)
            else:
                cur.execute(sql, params)

            if fetch:
                return cur.fetchall()

            conn.commit()
            return cur.lastrowid

        except Exception:
            conn.rollback()
            logger.exception("Database error")
            raise

        finally:
            cur.close()


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

    logger.info("Database initialized successfully.")


# =========================================================
# USERS
# =========================================================

def ensure_user(user):
    db_execute(
        """
        INSERT INTO users(
            user_id,
            username,
            first_name,
            points,
            created_at
        )
        VALUES (?, ?, ?, 0, ?)

        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
        """,
        (
            user.id,
            user.username or "",
            user.first_name or "",
            datetime.utcnow().isoformat(),
        ),
    )


# =========================================================
# GAME HELPERS
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


def parse_numbers(value):
    """
    Convert:
        '1,2,3'
    into:
        {1,2,3}
    """

    if not value:
        return set()

    result = set()

    for x in value.split(","):
        x = x.strip()

        if not x:
            continue

        try:
            result.add(int(x))
        except ValueError:
            logger.warning("Invalid number in database: %s", x)

    return result


def numbers_text(numbers):
    return ",".join(
        str(x)
        for x in sorted(numbers)
    )


# =========================================================
# BINGO CARD
# =========================================================

def make_card():
    """
    Standard Bingo card:

    B = 1-15
    I = 16-30
    N = 31-45
    G = 46-60
    O = 61-75

    Center = FREE
    """

    columns = [
        random.sample(range(1, 16), 5),
        random.sample(range(16, 31), 5),
        random.sample(range(31, 46), 5),
        random.sample(range(46, 61), 5),
        random.sample(range(61, 76), 5),
    ]

    grid = [
        [
            columns[c][r]
            for c in range(5)
        ]
        for r in range(5)
    ]

    # FREE center
    grid[2][2] = 0

    return grid


def is_bingo(grid, called):
    """
    Check rows, columns and diagonals.
    """

    # Rows
    for r in range(5):
        if all(
            grid[r][c] == 0 or
            grid[r][c] in called
            for c in range(5)
        ):
            return True

    # Columns
    for c in range(5):
        if all(
            grid[r][c] == 0 or
            grid[r][c] in called
            for r in range(5)
        ):
            return True

    # Main diagonal
    if all(
        grid[i][i] == 0 or
        grid[i][i] in called
        for i in range(5)
    ):
        return True

    # Other diagonal
    if all(
        grid[i][4 - i] == 0 or
        grid[i][4 - i] in called
        for i in range(5)
    ):
        return True

    return False


def card_text(grid, marked):
    lines = [
        "🎫 *Your Bingo Card*",
        "```",
        " B   I   N   G   O",
    ]

    for row in grid:
        cells = []

        for number in row:

            # FREE center
            if number == 0:
                cells.append(" ★ ")

            # Called number
            elif number in marked:
                cells.append(f"[{number:2}]")

            # Normal number
            else:
                cells.append(f" {number:2} ")

        lines.append(" ".join(cells))

    lines.append("```")
    lines.append(
        "Numbers in `[ ]` have been called. ★ = FREE"
    )

    return "\n".join(lines)


# =========================================================
# SAFE GRID LOADING
# =========================================================

def load_grid(value):
    """
    Safely convert database text back into a Bingo grid.
    """

    try:
        grid = ast.literal_eval(value)

        if not isinstance(grid, list):
            raise ValueError("Grid is not a list.")

        if len(grid) != 5:
            raise ValueError("Grid must have 5 rows.")

        for row in grid:
            if not isinstance(row, list) or len(row) != 5:
                raise ValueError("Each row must have 5 numbers.")

        return grid

    except Exception:
        logger.exception("Invalid Bingo card stored in database.")
        raise


# =========================================================
# ADMIN
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:
        ensure_user(update.effective_user)

        await update.message.reply_text(
            "🎉 Welcome to *Ethio Bingo*!\n\n"
            "Use /join to enter the current game.\n"
            "Use /card to view your card.\n"
            "Use /points to see your points.\n"
            "Use /help to see all commands.",
            parse_mode="Markdown",
        )

    except Exception:
        logger.exception("Error in /start")

        await update.message.reply_text(
            "❌ Something went wrong. Please try again."
        )


# =========================================================
# /JOIN
# =========================================================

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:
        ensure_user(update.effective_user)

        game = active_game()

        if not game:
            await update.message.reply_text(
                "⏳ No active game.\n\n"
                "Ask the admin to use /newgame."
            )
            return

        existing = get_card(
            game["id"],
            update.effective_user.id,
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

        # Use the numbers already called in case player joins
        # after some numbers have been called.
        called = parse_numbers(
            game["called_numbers"]
        )

        await update.message.reply_text(
            "🎫 You joined the game!\n\n"
            + card_text(grid, called),
            parse_mode="Markdown",
        )

    except Exception:
        logger.exception("Error in /join")

        await update.message.reply_text(
            "❌ Could not join the game. Please try again."
        )


# =========================================================
# /CARD
# =========================================================

async def card(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:
        ensure_user(update.effective_user)

        game = active_game()

        if not game:
            await update.message.reply_text(
                "⏳ There is no active game."
            )
            return

        c = get_card(
            game["id"],
            update.effective_user.id,
        )

        if not c:
            await update.message.reply_text(
                "❌ You are not in the game.\n"
                "Use /join first."
            )
            return

        grid = load_grid(c["numbers"])

        # IMPORTANT:
        # Use game called numbers too.
        # This makes /card always show the latest state.
        called = parse_numbers(
            game["called_numbers"]
        )

        marked = parse_numbers(
            c["marked"]
        )

        # Combine both
        marked.update(called)

        await update.message.reply_text(
            card_text(grid, marked),
            parse_mode="Markdown",
        )

    except Exception:
        logger.exception("Error in /card")

        await update.message.reply_text(
            "❌ Could not load your card."
        )


# =========================================================
# /POINTS
# =========================================================

async def points(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:
        ensure_user(update.effective_user)

        rows = db_execute(
            """
            SELECT points
            FROM users
            WHERE user_id=?
            """,
            (update.effective_user.id,),
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

    except Exception:
        logger.exception("Error in /points")

        await update.message.reply_text(
            "❌ Could not get your points."
        )


# =========================================================
# /NEWGAME
# =========================================================

async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:

        if not admin_only(update):
            await update.message.reply_text(
                "⛔ Admin only."
            )
            return

        # Close old games
        db_execute(
            "UPDATE games SET active=0 WHERE active=1"
        )

        # Create new game
        game_id = db_execute(
            """
            INSERT INTO games(
                active,
                called_numbers,
                created_at
            )
            VALUES(1, '', ?)
            """,
            (
                datetime.utcnow().isoformat(),
            ),
        )

        await update.message.reply_text(
            f"🎲 *New Bingo Game #{game_id} created!*\n\n"
            "Players can now use /join.",
            parse_mode="Markdown",
        )

        logger.info(
            "New game created: %s",
            game_id,
        )

    except Exception:
        logger.exception("Error in /newgame")

        await update.message.reply_text(
            "❌ Could not create a new game."
        )


# =========================================================
# /CALL
# =========================================================

async def call_number(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    try:

        logger.info(
            "/call received from user %s",
            update.effective_user.id
            if update.effective_user
            else "unknown",
        )

        # Admin check
        if not admin_only(update):

            logger.warning(
                "Unauthorized /call attempt by %s",
                update.effective_user.id
                if update.effective_user
                else "unknown",
            )

            await update.message.reply_text(
                "⛔ Admin only."
            )
            return

        # Get active game
        game = active_game()

        if not game:
            await update.message.reply_text(
                "⏳ Create a game first with /newgame."
            )
            return

        # Get already called numbers
        called = parse_numbers(
            game["called_numbers"]
        )

        logger.info(
            "Game #%s currently has %s called numbers.",
            game["id"],
            len(called),
        )

        # Numbers still available
        remaining = [
            number
            for number in range(1, 76)
            if number not in called
        ]

        if not remaining:

            await update.message.reply_text(
                "🏁 All 75 numbers have been called."
            )

            return

        # Pick random number
        number = random.choice(remaining)

        called.add(number)

        # Save called number
        db_execute(
            """
            UPDATE games
            SET called_numbers=?
            WHERE id=?
            """,
            (
                numbers_text(called),
                game["id"],
            ),
        )

        logger.info(
            "Game #%s called number: %s",
            game["id"],
            number,
        )

        # =================================================
        # CHECK ALL PLAYERS
        # =================================================

        winner = None

        cards = db_execute(
            """
            SELECT *
            FROM cards
            WHERE game_id=?
            """,
            (game["id"],),
            fetch=True,
        )

        logger.info(
            "Checking %s player cards.",
            len(cards),
        )

        for c in cards:

            try:

               
