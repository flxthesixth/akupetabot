# AkuPeta bot

Small Telegram task tracker and reminder bot, built with Python standard library and SQLite. User-facing commands and reminder phrases remain Indonesian. Times shown to users use WITA (Asia/Makassar).

## Commands

`/add <tugas>` adds task; `/list` lists tasks; `/done <nomor>` completes numbered task; `/priority <nomor>` toggles priority; `/delete <nomor>` deletes task; `/clear` deletes all tasks after confirmation. `/redeem <token>` activates access, `/status` checks expiry, `/help` shows Indonesian help. Ordinary text also adds task. Admin commands include `/gentoken 7|30`, `/users`, and `/deactivate`.

Reminder input uses Indonesian natural-language patterns, for example `ingatkan aku pergi besok jam 9 pagi` (remind me to go tomorrow at 9 AM). Recognized cues include `ingatkan`, `pengingat`, `reminder`, `alarm`, `alert`, and `ingat`; the grammar is limited, not a general language parser. Notifications go out 15 minutes before and at due time.

## Local check

Requires Python 3 with `zoneinfo` time-zone data. Run `python3 -I app.py --self-check` in this directory. This check uses a unique temporary SQLite directory, synthetic users and access tokens, and no Telegram polling. It does not need credentials. Do not run normal bot mode if another instance is polling the same Telegram token.

Normal operation reads `TELEGRAM_BOT_TOKEN`, `ACCESS_TOKEN_SALT`, and `ADMIN_IDS` from environment. No values belong in this repository. Runtime records (task text, Telegram user IDs and usernames, access-token hashes, and expiry data) live in local `tasks.sqlite3` beside `app.py`; treat database and WAL/SHM files as private. `.gitignore` excludes them and local credentials but does not erase files already tracked elsewhere. Review source, license and publication rights before publishing.
