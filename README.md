# Clean-Clean form handler

Microservice that receives form submissions from the Clean-Clean landing pages
and forwards them to a Telegram chat via Bot API.

## Endpoints

- `GET /` — service identity
- `GET /health` — health probe
- `POST /api/order` — accepts JSON with `name`, `phone`, `city`, `service`, `details`, `page`

## Required env vars

- `TELEGRAM_TOKEN` — Bot API token from @BotFather
- `TELEGRAM_CHAT_ID` — destination chat id (user, group, or channel)
- `ALLOWED_ORIGINS` — comma-separated origins for CORS (default: GitHub Pages site)

## Deploy on Render

- Build: `pip install -r requirements.txt`
- Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Runtime: Python 3.12 (pinned in `runtime.txt`)
