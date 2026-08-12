# Ethio Bingo Telegram Bot

Points-only Telegram Bingo demo.

## Files
- `bot.py` - main Telegram bot
- `requirements.txt` - Python dependency

## Environment variables
- `BOT_TOKEN` - Telegram BotFather token
- `ADMIN_ID` - your Telegram numeric user ID

Do not put the token directly in the code or commit it to GitHub.

## Commands
Users:
- `/start`
- `/join`
- `/card`
- `/points`

Admin:
- `/newgame`
- `/call`

The bot uses SQLite and awards 100 points to a Bingo winner.
