# Contributing to Telegram Sticker Loop Bot

Thanks for contributing. Here's how to get started.

## Setup

```bash
git clone https://github.com/lemonchikHere/telegram-sticker-loop-bot.git
cd telegram-sticker-loop-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install
cp .env.example .env  # then edit with your bot token
```

Or with Docker:

```bash
cp .env.example .env  # then edit with your bot token
docker compose up
```

## Running checks

```bash
# Python syntax check
python -m py_compile src/bot.py

# Node.js syntax check
node --check src/render_lottie.mjs
```

## Pull request guidelines

- Keep PRs focused — one feature or fix per PR.
- Follow the existing code style (dataclasses, type hints, async/await).
- If you add dependencies, update both `requirements.txt` and `Dockerfile`.
- Test manually with a real bot token before opening a PR.

## Architecture

- `src/bot.py` — the bot: handlers, inline menu, rate limiting, broadcasts, ffmpeg pipeline, SQLite user store.
- `src/render_lottie.mjs` — headless Lottie renderer (lottie-web + playwright-core), called as a subprocess from Python.
