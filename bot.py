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

DB_LOCK = threading.RLock()


def db_execute(sql, params=(), fetch=False, many=False):
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
            logger.warning(
                "Invalid number in database: %s",
                x,
            )

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

    grid[2][2] = 0

    return grid


def is_bingo(grid, called):
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

            if number == 0:
                cells.append(" ★ ")

            elif number in marked:
                cells.append(
                    f"[{number:2}]"
                )

            else:
                cells.append(
                    f" {number:2} "
                )

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
    try:
        grid = ast.literal_eval(value)

        if not isinstance(grid, list):
            raise ValueError(
                "Grid is not a list."
            )

        if len(grid) != 5:
            raise ValueError(
                "Grid must have 5 rows."
            )

        for row in grid:
            if not isinstance(row, list):
                raise ValueError(
                    "Invalid row."
                )

            if len(row) != 5:
                raise ValueError(
                    "Each row must have 5 numbers."
                )

        return grid

    except Exception:
        logger.exception(
            "Invalid Bingo card in database."
        )
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

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    try:
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

    except Exception:
        logger.exception(
            "Error in /start"
        )

        await update.message.reply_text(
            "❌ Something went wrong. Please try again."
        )


# =========================================================
# /JOIN
# =========================================================

async def join(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    try:
        ensure_user(
            update.effective_user
        )

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

        called = parse_numbers(
            game["called_numbers"]
        )

        await update.message.reply_text(
            "🎫 You joined the game!\n\n"
            + card_text(
                grid,
                called,
            ),
            parse_mode="Markdown",
        )

    except Exception:
        logger.exception(
            "Error in /join"
        )

        await update.message.reply_text(
            "❌ Could not join the game. Please try again."
        )


# =========================================================
# /CARD
# =========================================================

async def card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    try:
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
            update.effective_user.id,
        )

        if not c:
            await update.message.reply_text(
                "❌ You are not in the game.\n"
                "Use /join first."
            )
            return

        grid = load_grid(
            c["numbers"]
        )

        called = parse_numbers(
            game["called_numbers"]
        )

        marked = parse_numbers(
            c["marked"]
        )

        marked.update(called)

        await update.message.reply_text(
            card_text(
                grid,
                marked,
            ),
            parse_mode="Markdown",
        )

    except Exception:
        logger.exception(
            "Error in /card"
        )

        await update.message.reply_text(
            "❌ Could not load your card."
        )


# =========================================================
# /POINTS
# =========================================================

async def points(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    try:
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

    except Exception:
        logger.exception(
            "Error in /points"
        )

        await update.message.reply_text(
            "❌ Could not get your points."
        )


# =========================================================
# /LEADERBOARD
# =========================================================

async def leaderboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
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
            ORDER BY points DESC, user_id ASC
            LIMIT 10
            """,
            fetch=True,
        )

        if not rows:
            await update.message.reply_text(
                "🏆 *Ethio Bingo Leaderboard*\n\n"
                "No players have points yet.",
                parse_mode="Markdown",
            )
            return

        lines = [
            "🏆 *Ethio Bingo Leaderboard*",
            "",
        ]

        medals = [
            "🥇",
            "🥈",
            "🥉",
        ]

        for index, row in enumerate(rows):

            if index < 3:
                position = medals[index]
            else:
                position = f"{index + 1}."

            name = (
                row["first_name"]
                or row["username"]
                or str(row["user_id"])
            )

            lines.append(
                f"{position} {name} — "
                f"💰 *{row['points']} points*"
            )

        lines.append("")
        lines.append(
            "🎯 Top 10 players"
        )

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
        )

    except Exception:
        logger.exception(
            "Error in /leaderboard"
        )

        await update.message.reply_text(
            "❌ Could not load leaderboard."
        )


# =========================================================
# /NEWGAME
# =========================================================

async def newgame(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    try:

        if not admin_only(update):
            await update.message.reply_text(
                "⛔ Admin only."
            )
            return

        db_execute(
            """
            UPDATE games
            SET active=0
            WHERE active=1
            """
        )

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
        logger.exception(
            "Error in /newgame"
        )

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

        if not admin_only(update):

            await update.message.reply_text(
                "⛔ Admin only."
            )
            return

        game = active_game()

        if not game:
            await update.message.reply_text(
                "⏳ Create a game first with /newgame."
            )
            return

        called = parse_numbers(
            game["called_numbers"]
        )

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

        number = random.choice(
            remaining
        )

        called.add(number)

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

        winner = None

        cards = db_execute(
            """
            SELECT *
            FROM cards
            WHERE game_id=?
            """,
            (
                game["id"],
            ),
            fetch=True,
        )

        for c in cards:

            try:

                grid = load_grid(
                    c["numbers"]
                )

                marked = parse_numbers(
                    c["marked"]
                )

                marked.add(number)

                db_execute(
                    """
                    UPDATE cards
                    SET marked=?
                    WHERE game_id=?
                    AND user_id=?
                    """,
                    (
                        numbers_text(marked),
                        game["id"],
                        c["user_id"],
                    ),
                )

                if is_bingo(
                    grid,
                    marked,
                ):

                    winner = c["user_id"]

                    logger.info(
                        "BINGO winner: %s",
                        winner,
                    )

                    break

            except Exception:
                logger.exception(
                    "Error checking card for user %s",
                    c["user_id"],
                )

        # =================================================
        # WINNER
        # =================================================

        if winner:

            db_execute(
                """
                UPDATE users
                SET points=points+100
                WHERE user_id=?
                """,
                (
                    winner,
                ),
            )

            db_execute(
                """
                UPDATE games
                SET active=0
                WHERE id=?
                """,
                (
                    game["id"],
                ),
            )

            await update.message.reply_text(
                f"🔔 *BINGO NUMBER: {number}*\n\n"
                "🏆 *We have a winner!*\n"
                f"👤 Player ID: `{winner}`\n"
                "💰 +100 points\n\n"
                "🎉 Game finished.\n"
                "Admin can use /newgame for another game.",
                parse_mode="Markdown",
            )

            return

        # =================================================
        # NO WINNER
        # =================================================

        await update.message.reply_text(
            f"🔔 *BINGO NUMBER: {number}*\n"
            f"📢 Called numbers: {len(called)}/75",
            parse_mode="Markdown",
        )

    except Exception:

        logger.exception(
            "CRITICAL ERROR in /call"
        )

        try:
            await update.message.reply_text(
                "❌ Error while calling the number.\n"
                "Check Render logs for details."
            )

        except Exception:
            logger.exception(
                "Could not send error message."
            )


# =========================================================
# /HELP
# =========================================================

async def help_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🎲 *Ethio Bingo Commands*\n\n"
        "/start - Register and welcome\n"
        "/join - Join the active game\n"
        "/card - Show your card\n"
        "/points - Show your points\n"
        "/leaderboard - Top 10 players\n"
        "/help - Show commands\n\n"
        "👑 *Admin Commands*\n"
        "/newgame - Create a new game\n"
        "/call - Call the next number",
        parse_mode="Markdown",
    )


# =========================================================
# RENDER WEB SERVER
# =========================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return (
        "Ethio Bingo Bot is running! 🎲",
        200,
    )


@web_app.route("/health")
def health():
    return (
        "OK",
        200,
    )


def run_web_server():

    port = int(
        os.environ.get(
            "PORT",
            10000,
        )
    )

    logger.info(
        "Starting web server on port %s",
        port,
    )

    web_app.run(
        host="0.0.0.0",
        port=port,
    )


# =========================================================
# MAIN
# =========================================================

def main():

    logger.info(
        "Starting Ethio Bingo Bot..."
    )

    init_db()

    web_thread = threading.Thread(
        target=run_web_server,
        daemon=True,
    )

    web_thread.start()

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "join",
            join,
        )
    )

    app.add_handler(
        CommandHandler(
            "card",
            card,
        )
    )

    app.add_handler(
        CommandHandler(
            "points",
            points,
        )
    )

    app.add_handler(
        CommandHandler(
            "leaderboard",
            leaderboard,
        )
    )

    app.add_handler(
        CommandHandler(
            "newgame",
            newgame,
        )
    )

    app.add_handler(
        CommandHandler(
            "call",
            call_number,
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_cmd,
        )
    )

    logger.info(
        "Ethio Bingo bot is ready."
    )

    app.run_polling(
        drop_pending_updates=True
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
