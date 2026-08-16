import os
import random
import sqlite3
import logging
import threading
import ast
from datetime import datetime, timezone

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================================================
# SETTINGS
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
DB_FILE = os.getenv("DB_FILE", "bingo.db")

# Virtual points awarded to the winner.
WIN_POINTS = int(os.getenv("WIN_POINTS", "100"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required.")

try:
    ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else None
except ValueError:
    raise RuntimeError("ADMIN_ID must be a Telegram numeric user ID.")


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
)

conn.row_factory = sqlite3.Row

db_lock = threading.Lock()


def db_execute(sql, params=(), fetch=False, many=False):
    with db_lock:
        cur = conn.cursor()

        if many:
            cur.executemany(sql, params)
        else:
            cur.execute(sql, params)

        conn.commit()

        if fetch:
            return cur.fetchall()

        return cur.lastrowid


def now():
    return datetime.now(timezone.utc).isoformat()


def init_db():

    db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            points INTEGER NOT NULL DEFAULT 0,
            games_played INTEGER NOT NULL DEFAULT 0,
            games_won INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            active INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'waiting',
            called_numbers TEXT NOT NULL DEFAULT '',
            winner_id INTEGER,
            created_at TEXT NOT NULL,
            finished_at TEXT
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

    db_execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            reason TEXT NOT NULL,
            game_id INTEGER,
            created_at TEXT NOT NULL
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            user_id INTEGER PRIMARY KEY,
            referrer_id INTEGER,
            created_at TEXT NOT NULL
        )
    """)


# =========================================================
# USERS
# =========================================================

def ensure_user(user):

    db_execute(
        """
        INSERT INTO users (
            user_id,
            username,
            first_name,
            created_at
        )
        VALUES (?, ?, ?, ?)

        ON CONFLICT(user_id)
        DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
        """,
        (
            user.id,
            user.username or "",
            user.first_name or "",
            now(),
        ),
    )


def get_user(user_id):

    rows = db_execute(
        """
        SELECT *
        FROM users
        WHERE user_id=?
        """,
        (user_id,),
        fetch=True,
    )

    return rows[0] if rows else None


def get_user_name(user_id):

    user = get_user(user_id)

    if not user:
        return f"Player {user_id}"

    if user["first_name"]:
        return user["first_name"]

    if user["username"]:
        return f"@{user['username']}"

    return f"Player {user_id}"


def change_points(
    user_id,
    amount,
    reason,
    game_id=None,
):

    db_execute(
        """
        UPDATE users
        SET points = points + ?
        WHERE user_id=?
        """,
        (
            amount,
            user_id,
        ),
    )

    db_execute(
        """
        INSERT INTO transactions (
            user_id,
            amount,
            reason,
            game_id,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            user_id,
            amount,
            reason,
            game_id,
            now(),
        ),
    )


# =========================================================
# GAMES
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
        (
            game_id,
            user_id,
        ),
        fetch=True,
    )

    return rows[0] if rows else None


def game_players(game_id):

    return db_execute(
        """
        SELECT user_id
        FROM cards
        WHERE game_id=?
        """,
        (game_id,),
        fetch=True,
    )


def create_game():

    old_game = active_game()

    if old_game:

        db_execute(
            """
            UPDATE games
            SET
                active=0,
                status='cancelled',
                finished_at=?
            WHERE id=?
            """,
            (
                now(),
                old_game["id"],
            ),
        )

    return db_execute(
        """
        INSERT INTO games (
            active,
            status,
            called_numbers,
            created_at
        )
        VALUES (1, 'waiting', '', ?)
        """,
        (now(),),
    )


def close_game(
    game_id,
    status="finished",
    winner_id=None,
):

    db_execute(
        """
        UPDATE games
        SET
            active=0,
            status=?,
            winner_id=?,
            finished_at=?
        WHERE id=?
        """,
        (
            status,
            winner_id,
            now(),
            game_id,
        ),
    )


# =========================================================
# BINGO CARD
# =========================================================

def make_card():

    columns = [

        random.sample(
            range(1, 16),
            5,
        ),

        random.sample(
            range(16, 31),
            5,
        ),

        random.sample(
            range(31, 46),
            5,
        ),

        random.sample(
            range(46, 61),
            5,
        ),

        random.sample(
            range(61, 76),
            5,
        ),
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
        str(number)
        for number in sorted(numbers)
    )


# =========================================================
# BINGO CHECK
# =========================================================

def is_bingo(grid, called):

    # Rows
    for row in range(5):

        if all(
            grid[row][column] == 0
            or grid[row][column] in called
            for column in range(5)
        ):
            return True

    # Columns
    for column in range(5):

        if all(
            grid[row][column] == 0
            or grid[row][column] in called
            for row in range(5)
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

        lines.append(
            " ".join(cells)
        )

    lines.append("```")

    lines.append(
        "Numbers in `[ ]` have been called."
    )

    lines.append(
        "★ = FREE"
    )

    return "\n".join(lines)


# =========================================================
# KEYBOARD
# =========================================================

def main_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎮 Join Game",
                    callback_data="join",
                ),
                InlineKeyboardButton(
                    "🎫 My Card",
                    callback_data="card",
                ),
            ],
            [
                InlineKeyboardButton(
                    "💰 Points",
                    callback_data="points",
                ),
                InlineKeyboardButton(
                    "🏆 Leaderboard",
                    callback_data="leaderboard",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📊 My Stats",
                    callback_data="stats",
                ),
                InlineKeyboardButton(
                    "ℹ️ Help",
                    callback_data="help",
                ),
            ],
        ]
    )


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

    user = update.effective_user

    ensure_user(user)

    # Referral:
    if context.args:

        referral_code = context.args[0]

        if referral_code.isdigit():

            referrer_id = int(
                referral_code
            )

            if (
                referrer_id != user.id
                and get_user(referrer_id)
            ):

                db_execute(
                    """
                    INSERT OR IGNORE INTO referrals (
                        user_id,
                        referrer_id,
                        created_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        user.id,
                        referrer_id,
                        now(),
                    ),
                )

    await update.message.reply_text(

        "🎉 *Welcome to Ethio Bingo V2!*\n\n"

        "🎮 Multiplayer Bingo\n"
        "💰 Virtual Points\n"
        "🏆 Leaderboard\n"
        "📊 Player Statistics\n"
        "🎁 Referral System\n\n"

        "⚠️ This version uses virtual points only.\n"
        "Real-money deposits and withdrawals are disabled.\n\n"

        "Choose an option below:",

        parse_mode="Markdown",

        reply_markup=main_keyboard(),
    )


# =========================================================
# /HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(

        "📖 *Ethio Bingo Commands*\n\n"

        "/start — Main menu\n"
        "/join — Join active game\n"
        "/card — Show your card\n"
        "/points — Show points\n"
        "/stats — Your statistics\n"
        "/history — Game history\n"
        "/leaderboard — Top players\n"
        "/help — Help\n\n"

        "👨‍💼 *Admin Commands*\n\n"

        "/newgame\n"
        "/startgame\n"
        "/call\n"
        "/stopgame\n"
        "/users\n"
        "/statsadmin\n"
        "/givepoints USER_ID AMOUNT\n"
        "/removepoints USER_ID AMOUNT",

        parse_mode="Markdown",
    )


# =========================================================
# /JOIN
# =========================================================

async def join(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    ensure_user(user)

    game = active_game()

    if not game:

        await update.message.reply_text(
            "⏳ No active game.\n\n"
            "Ask the admin to create one."
        )

        return

    if game["status"] != "waiting":

        await update.message.reply_text(
            "🔒 This game has already started."
        )

        return

    existing = get_card(
        game["id"],
        user.id,
    )

    if existing:

        await update.message.reply_text(
            "✅ You are already in this game.\n"
            "Use /card."
        )

        return

    grid = make_card()

    db_execute(
        """
        INSERT INTO cards (
            game_id,
            user_id,
            numbers,
            marked
        )
        VALUES (?, ?, ?, '')
        """,
        (
            game["id"],
            user.id,
            repr(grid),
        ),
    )

    await update.message.reply_text(

        "🎫 *You joined the game!*\n\n"

        + card_text(
            grid,
            set(),
        ),

        parse_mode="Markdown",
    )


# =========================================================
# /CARD
# =========================================================

async def card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    ensure_user(user)

    game = active_game()

    if not game:

        await update.message.reply_text(
            "⏳ No active game."
        )

        return

    current_card = get_card(
        game["id"],
        user.id,
    )

    if not current_card:

        await update.message.reply_text(
            "❌ You are not in this game.\n"
            "Use /join."
        )

        return

    grid = parse_grid(
        current_card["numbers"]
    )

    marked = parse_numbers(
        current_card["marked"]
    )

    await update.message.reply_text(

        card_text(
            grid,
            marked,
        ),

        parse_mode="Markdown",
    )


# =========================================================
# /POINTS
# =========================================================

async def points(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    ensure_user(user)

    row = get_user(user.id)

    await update.message.reply_text(

        f"💰 *Your Balance*\n\n"
        f"⭐ Points: *{row['points']}*",

        parse_mode="Markdown",
    )


# =========================================================
# /STATS
# =========================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    ensure_user(user)

    row = get_user(user.id)

    await update.message.reply_text(

        f"📊 *Your Statistics*\n\n"

        f"💰 Points: {row['points']}\n"
        f"🎮 Games Played: {row['games_played']}\n"
        f"🏆 Games Won: {row['games_won']}\n",

        parse_mode="Markdown",
    )


# =========================================================
# /HISTORY
# =========================================================

async def history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    ensure_user(user)

    rows = db_execute(
        """
        SELECT
            g.id,
            g.status,
            g.winner_id
        FROM games g

        INNER JOIN cards c
            ON c.game_id = g.id

        WHERE c.user_id=?

        ORDER BY g.id DESC

        LIMIT 10
        """,
        (
            user.id,
        ),
        fetch=True,
    )

    if not rows:

        await update.message.reply_text(
            "🧾 You have no game history yet."
        )

        return

    text = "🧾 *Recent Games*\n\n"

    for row in rows:

        if row["winner_id"] == user.id:

            result = "🏆 WIN"

        else:

            result = row["status"].upper()

        text += (
            f"🎮 Game #{row['id']} — "
            f"{result}\n"
        )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
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
                first_name,
                username,
                points,
                games_won

            FROM users

            WHERE points > 0
               OR games_won > 0

            ORDER BY
                points DESC,
                games_won DESC

            LIMIT 10
            """,
            fetch=True,
        )

        if not rows:

            await update.message.reply_text(
                "🏆 Leaderboard\n\n"
                "No players have points yet."
            )

            return

        medals = [
            "🥇",
            "🥈",
            "🥉",
        ]

        text = (
            "🏆 *Ethio Bingo Leaderboard*\n\n"
        )

        for i, row in enumerate(rows):

            name = (
                row["first_name"]
                or row["username"]
                or "Player"
            )

            prefix = (
                medals[i]
                if i < 3
                else f"{i + 1}."
            )

            text += (
                f"{prefix} "
                f"{name} — "
                f"💰 {row['points']} "
                f"| 🏆 {row['games_won']}\n"
            )

        await update.message.reply_text(
            text,
            parse_mode="Markdown",
        )

    except Exception:

        logger.exception(
            "Leaderboard error"
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

    if not admin_only(update):

        await update.message.reply_text(
            "⛔ Admin only."
        )

        return

    game_id = create_game()

    await update.message.reply_text(

        f"🆕 *Game #{game_id} Created!*\n\n"

        "Players can now use /join.",

        parse_mode="Markdown",
    )


# =========================================================
# /STARTGAME
# =========================================================

async def startgame(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not admin_only(update):

        await update.message.reply_text(
            "⛔ Admin only."
        )

        return

    game = active_game()

    if not game:

        await update.message.reply_text(
            "⏳ No active game."
        )

        return

    players = game_players(
        game["id"]
    )

    if not players:

        await update.message.reply_text(
            "👥 No players have joined."
        )

        return

    db_execute(
        """
        UPDATE games
        SET status='playing'
        WHERE id=?
        """,
        (
            game["id"],
        ),
    )

    for player in players:

        db_execute(
            """
            UPDATE users
            SET games_played = games_played + 1
            WHERE user_id=?
            """,
            (
                player["user_id"],
            ),
        )

    await update.message.reply_text(

        f"▶️ *Game #{game['id']} Started!*\n\n"

        f"👥 Players: {len(players)}\n\n"

        "Use /call to call the next number.",

        parse_mode="Markdown",
    )


# =========================================================
# /CALL
# =========================================================

async def call_number(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not admin_only(update):

        await update.message.reply_text(
            "⛔ Admin only."
        )

        return

    game = active_game()

    if not game:

        await update.message.reply_text(
            "⏳ No active game."
        )

        return

    if game["status"] != "playing":

        await update.message.reply_text(
            "▶️ Start the game first with /startgame."
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

        close_game(
            game["id"],
            "finished",
        )

        await update.message.reply_text(
            "🏁 All numbers have been called."
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

    if number <= 15:
        letter = "B"

    elif number <= 30:
        letter = "I"

    elif number <= 45:
        letter = "N"

    elif number <= 60:
        letter = "G"

    else:
        letter = "O"

    winner = None

    players = game_players(
        game["id"]
    )

    for player in players:

        user_id = player["user_id"]

        current_card = get_card(
            game["id"],
            user_id,
        )

        if not current_card:
            continue

        grid = parse_grid(
            current_card["numbers"]
        )

        marked = parse_numbers(
            current_card["marked"]
        )

        if number in {
            value
            for row in grid
            for value in row
            if value != 0
        }:

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
                user_id,
            ),
        )

        if is_bingo(
            grid,
            called,
        ):

            winner = user_id

            break

    await update.message.reply_text(

        f"🔔 *{letter} - {number}*\n\n"

        f"🔢 Numbers called: "
        f"{len(called)}",

        parse_mode="Markdown",
    )

    if winner:

        change_points(
            winner,
            WIN_POINTS,
            "Bingo win",
            game["id"],
        )

        db_execute(
            """
            UPDATE users
            SET games_won = games_won + 1
            WHERE user_id=?
            """,
            (
                winner,
            ),
        )

        close_game(
            game["id"],
            "finished",
            winner,
        )

        try:

            await context.bot.send_message(

                chat_id=winner,

                text=(
                    "🎉 *BINGO!*\n\n"
                    f"🏆 You won "
                    f"{WIN_POINTS} virtual points!"
                ),

                parse_mode="Markdown",
            )

        except Exception:

            logger.exception(
                "Winner notification failed"
            )

        await update.message.reply_text(

            f"🎉 *BINGO!*\n\n"

            f"🏆 Winner: "
            f"{get_user_name(winner)}\n\n"

            f"💰 Prize: "
            f"{WIN_POINTS} virtual points\n\n"

            f"🏁 Game #{game['id']} finished.",

            parse_mode="Markdown",
        )


# =========================================================
# /STOPGAME
# =========================================================

async def stopgame(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not admin_only(update):

        await update.message.reply_text(
            "⛔ Admin only."
        )

        return

    game = active_game()

    if not game:

        await update.message.reply_text(
            "⏳ No active game."
        )

        return

    close_game(
        game["id"],
        "stopped",
    )

    await update.message.reply_text(
        f"🛑 Game #{game['id']} stopped."
    )


# =========================================================
# /USERS
# =========================================================

async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not admin_only(update):

        await update.message.reply_text(
            "⛔ Admin only."
        )

        return

    row = db_execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        """,
        fetch=True,
    )[0]

    await update.message.reply_text(
        f"👥 Registered users: {row['count']}"
    )


# =========================================================
# /STATSADMIN
# =========================================================

async def statsadmin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not admin_only(update):

        await update.message.reply_text(
            "⛔ Admin only."
        )

        return

    users = db_execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        """,
        fetch=True,
    )[0]["count"]

    games = db_execute(
        """
        SELECT COUNT(*) AS count
        FROM games
        """,
        fetch=True,
    )[0]["count"]

    winners = db_execute(
        """
        SELECT COUNT(*) AS count
        FROM games
        WHERE winner_id IS NOT NULL
        """,
        fetch=True,
    )[0]["count"]

    await update.message.reply_text(

        f"📊 *Admin Statistics*\n\n"

        f"👥 Users: {users}\n"
        f"🎮 Games: {games}\n"
        f"🏆 Games with winner: {winners}",

        parse_mode="Markdown",
    )


# =========================================================
# /GIVEPOINTS
# =========================================================

async def givepoints(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not admin_only(update):

        await update.message.reply_text(
            "⛔ Admin only."
        )

        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "/givepoints USER_ID AMOUNT"
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

        amount = int(
            context.args[1]
        )

        if amount <= 0:
            raise ValueError

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid user ID or amount."
        )

        return

    if not get_user(user_id):

        await update.message.reply_text(
            "❌ User not found."
        )

        return

    change_points(
        user_id,
        amount,
        "Admin credit",
    )

    await update.message.reply_text(

        f"✅ Added "
        f"{amount} points to "
        f"{user_id}."
    )


# =========================================================
# /REMOVEPOINTS
# =========================================================

async def removepoints(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not admin_only(update):

        await update.message.reply_text(
            "⛔ Admin only."
        )

        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "/removepoints USER_ID AMOUNT"
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

        amount = int(
            context.args[1]
        )

        if amount <= 0:
            raise ValueError

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid user ID or amount."
        )

        return

    user = get_user(user_id)

    if not user:

        await update.message.reply_text(
            "❌ User not found."
        )

        return

    amount = min(
        amount,
        user["points"],
    )

    change_points(
        user_id,
        -amount,
        "Admin debit",
    )

    await update.message.reply_text(

        f"✅ Removed "
        f"{amount} points."
    )


# =========================================================
# CALLBACK BUTTONS
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    ensure_user(user)

    if query.data == "join":

        game = active_game()

        if not game:

            await query.message.reply_text(
                "⏳ No active game."
            )

            return

        if game["status"] != "waiting":

            await query.message.reply_text(
                "🔒 Game already started."
            )

            return

        if get_card(
            game["id"],
            user.id,
        ):

            await query.message.reply_text(
                "✅ You are already in the game."
            )

            return

        grid = make_card()

        db_execute(
            """
            INSERT INTO cards (
                game_id,
                user_id,
                numbers,
                marked
            )
            VALUES (?, ?, ?, '')
            """,
            (
                game["id"],
                user.id,
                repr(grid),
            ),
        )

        await query.message.reply_text(

            "🎫 *You joined!*\n\n"

            + card_text(
                grid,
                set(),
            ),

            parse_mode="Markdown",
        )

    elif query.data == "card":

        game = active_game()

        if not game:

            await query.message.reply_text(
                "⏳ No active game."
            )

            return

        current_card = get_card(
            game["id"],
            user.id,
        )

        if not current_card:

            await query.message.reply_text(
                "Use /join first."
            )

            return

        await query.message.reply_text(

            card_text(
                parse_grid(
                    current_card["numbers"]
                ),
                parse_numbers(
                    current_card["marked"]
                ),
            ),

            parse_mode="Markdown",
        )

    elif query.data == "points":

        row = get_user(
            user.id
        )

        await query.message.reply_text(
            f"💰 Points: {row['points']}"
        )

    elif query.data == "leaderboard":

        rows = db_execute(
            """
            SELECT
                first_name,
                username,
                points

            FROM users

            WHERE points > 0

            ORDER BY points DESC

            LIMIT 10
            """,
            fetch=True,
        )

        text = (
            "🏆 *Leaderboard*\n\n"
        )

        if not rows:

            text += (
                "No points yet."
            )

        else:

            for i, row in enumerate(
                rows,
                1,
            ):

                name = (
                    row["first_name"]
                    or row["username"]
                    or "Player"
                )

                text += (
                    f"{i}. {name} — "
                    f"{row['points']}\n"
                )

        await query.message.reply_text(
            text,
            parse_mode="Markdown",
        )

    elif query.data == "stats":

        row = get_user(
            user.id
        )

        await query.message.reply_text(

            f"📊 *Your Stats*\n\n"

            f"💰 Points: "
            f"{row['points']}\n"

            f"🎮 Played: "
            f"{row['games_played']}\n"

            f"🏆 Won: "
            f"{row['games_won']}",

            parse_mode="Markdown",
        )

    elif query.data == "help":

        await query.message.reply_text(

            "ℹ️ *How to play*\n\n"

            "1️⃣ Join the active game\n"
            "2️⃣ Get your Bingo card\n"
            "3️⃣ Admin calls numbers\n"
            "4️⃣ Numbers are automatically marked\n"
            "5️⃣ Complete a row, column or diagonal\n"
            "6️⃣ The first valid Bingo wins\n\n"

            "💰 This version uses virtual points only.",

            parse_mode="Markdown",
        )


# =========================================================
# HEALTH SERVER
# =========================================================

app = Flask(__name__)


@app.get("/")
def health():

    return "Ethio Bingo Bot is running."


def run_health_server():

    port = int(
        os.getenv(
            "PORT",
            "8080",
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )


# =========================================================
# MAIN
# =========================================================

def main():

    init_db()

    # HTTP health server for hosting platforms.
    threading.Thread(
        target=run_health_server,
        daemon=True,
    ).start()

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # User commands
    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "join",
            join,
        )
    )

    application.add_handler(
        CommandHandler(
            "card",
            card,
        )
    )

    application.add_handler(
        CommandHandler(
            "points",
            points,
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats,
        )
    )

    application.add_handler(
        CommandHandler(
            "history",
            history,
        )
    )

    application.add_handler(
        CommandHandler(
            "leaderboard",
            leaderboard,
        )
    )

    # Admin commands
    application.add_handler(
        CommandHandler(
            "newgame",
            newgame,
        )
    )

    application.add_handler(
        CommandHandler(
            "startgame",
            startgame,
        )
    )

    application.add_handler(
        CommandHandler(
            "call",
            call_number,
        )
    )

    application.add_handler(
        CommandHandler(
            "stopgame",
            stopgame,
        )
    )

    application.add_handler(
        CommandHandler(
            "users",
            users_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "statsadmin",
            statsadmin,
        )
    )

    application.add_handler(
        CommandHandler(
            "givepoints",
            givepoints,
        )
    )

    application.add_handler(
        CommandHandler(
            "removepoints",
            removepoints,
        )
    )

    # Inline buttons
    application.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    logger.info(
        "Ethio Bingo V2 starting..."
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
