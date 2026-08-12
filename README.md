# Chaweta-style Telegram Bingo Bot (Points Demo)

A simple Telegram Bingo bot inspired by the game flow you described.

## Included
- User registration
- 5x5 Bingo cards
- Join game
- Random numbers 1-75
- Automatic card marking
- Row/column/diagonal Bingo detection
- Winner gets 100 points
- SQLite database
- Admin commands

## Important
This version is **points-only**. It does not process deposits, withdrawals, cash betting, or gambling payments.

## Setup

1. Create a Telegram bot using `@BotFather`.
2. Copy the bot token.
3. Install Python 3.10+.
4. Install dependencies:

```bash
pip install -r requirements.txt
```

5. Set environment variables:

Linux/Android Termux:
```bash
export BOT_TOKEN="YOUR_BOT_TOKEN"
export ADMIN_ID="YOUR_TELEGRAM_USER_ID"
python bot.py
```

Windows PowerShell:
```powershell
$env:BOT_TOKEN="YOUR_BOT_TOKEN"
$env:ADMIN_ID="YOUR_TELEGRAM_USER_ID"
python bot.py
```

## Admin commands
- `/newgame` - create a game
- `/call` - call the next random number

## User commands
- `/start`
- `/join`
- `/card`
- `/points`

For a production version, add authentication, stronger database handling, anti-abuse controls, game rooms, payment compliance where applicable, and a web/admin dashboard.
