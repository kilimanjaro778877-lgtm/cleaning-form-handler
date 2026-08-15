# Clean-Clean form handler

Microservice that receives form submissions from the Clean-Clean / ЧистоТак
landing pages and forwards them to Telegram via Bot API. Also stores ЧистоТак
reviews in Postgres (encrypted PII columns) with moderation via Telegram
inline buttons.

## Endpoints

- `GET /` — service identity
- `GET /health` — health probe
- `POST /api/order` — Clean-Clean lead: `name`, `phone`, `city`, `service`, `details`, `page`
- `POST /api/chistotak-order` — ЧистоТак lead, same shape, separate bot/chat
- `POST /api/chistotak-review` — submit a review: `name`, `rating` (1-5), `text`, `city`, `service`, `page`. No phone. Goes to `pending`, sent to Telegram with ✅/❌ buttons for moderation. 503 if the reviews DB isn't configured/reachable.
- `GET /api/chistotak-reviews` — public: approved reviews only (`{reviews: [...]}`, empty list if DB unavailable — never errors)
- `POST /telegram-webhook` — Telegram bot webhook (approve/reject buttons + reply-to-review capture). 404 unless `TELEGRAM_WEBHOOK_SECRET` is set and the request carries a matching `X-Telegram-Bot-Api-Secret-Token` header.

`/docs` and `/openapi.json` are **disabled by default** (would otherwise hand
out a full API map, including moderation endpoints, to anyone). Set
`ENABLE_DOCS=1` for local dev only — never in the Render env vars.

## Required env vars

- `TELEGRAM_TOKEN` — Bot API token from @BotFather
- `TELEGRAM_CHAT_ID` — destination chat id (user, group, or channel)
- `CHISTOTAK_TELEGRAM_TOKEN` / `CHISTOTAK_TELEGRAM_CHAT_ID` — separate ЧистоТак bot/chat (falls back to the vars above if unset)
- `ALLOWED_ORIGINS` — comma-separated origins for CORS (default: GitHub Pages site)
- `TIKTOK_ACCESS_TOKEN` — **rotate this in TikTok Business Manager and set it here.** The old value used to be hardcoded in `app.py` in this (public) repo, so it must be treated as already leaked.

### Reviews feature (all three required together, or the feature quietly disables itself — nothing else breaks)

- `DATABASE_URL` — Render Postgres connection string (Render injects this automatically once you attach a Postgres instance to this service)
- `REVIEWS_ENC_KEY` — 32-byte key, base64. Generate once, store ONLY here, never in git/chat:
  ```
  python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
  ```
  Losing this key = every stored review becomes permanently unreadable (by design — that's also what makes a stolen DB backup worthless without it). Back it up somewhere safe outside Render (password manager), not just in the Render dashboard.
- `IP_HASH_SALT` — any random string (e.g. same generator as above, or a passphrase). Used to hash submitter IPs for rate-limiting, so raw IPs are never stored.
- `TELEGRAM_WEBHOOK_SECRET` — random string, matched against Telegram's `X-Telegram-Bot-Api-Secret-Token` header. Generate similarly, then register the webhook once (replace values, run from your own machine — don't paste the bot token into chat):
  ```
  curl -s "https://api.telegram.org/bot<CHISTOTAK_TELEGRAM_TOKEN>/setWebhook" \
    -d "url=https://<your-render-service>.onrender.com/telegram-webhook" \
    -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
  ```

## Reviews: how moderation actually works

1. Visitor submits a review on the site → stored `pending`, encrypted, rate-limited (max 3/hour per IP-hash) → notification lands in the ЧистоТак Telegram chat with **✅ Опублікувати / ❌ Відхилити** buttons.
2. Tap a button → status updates in the DB, buttons disappear, message gets a status line. No web admin panel exists — nothing browser-facing to attack.
3. To reply to a review: reply (Telegram's native Reply, not a new message) to that notification with your answer. The bot captures it, encrypts and stores it as the review's reply, and auto-approves the review if it was still pending.
4. `GET /api/chistotak-reviews` only ever returns `approved` rows — `pending`/`rejected` never leave the DB via any public endpoint.

## Threat model (why it's built this way)

- **DB dump/backup theft, leaked `DATABASE_URL`, compromised Render account** → reviewer name/text are AES-256-GCM encrypted at the application layer with a key that lives *only* in `REVIEWS_ENC_KEY` (Render env var), never in the database or git. A raw table dump is ciphertext without it.
- **SQL injection** → every query in `db.py` is parameterized (`$1, $2, ...`); no string-built SQL anywhere.
- **Public reviews endpoint leaking unapproved/PII data** → `GET /api/chistotak-reviews` filters `status = 'approved'` server-side and returns only display fields (no `ip_hash`, no internal ids beyond the review id).
- **Unauthenticated moderation** → there is no browser-reachable admin endpoint at all; moderation is Telegram-only (buttons + reply capture), gated by `TELEGRAM_WEBHOOK_SECRET` checked with a constant-time comparison (`secrets.compare_digest`) so it can't be brute-forced via timing.
- **`/docs` handing out the API map** → disabled unless `ENABLE_DOCS=1` is set explicitly (local dev only).
- **Spam/abuse flooding the pending queue** → existing honeypot field reused + hard rate-limit (3 reviews/hour per hashed IP); nothing publishes without a human tapping ✅.
- **XSS via review text** → the frontend renders review content with `textContent`, never `innerHTML`, so injected HTML/script in a review body can't execute.
- **Missing reviews config crashing the whole service** → `REVIEWS_ENC_KEY`/`IP_HASH_SALT`/`DATABASE_URL` are checked once at startup, not per-request; if any is missing, only the reviews endpoints degrade (503 / empty list) — order-form endpoints (both sites) keep working regardless.
- **Residual risk, accepted:** plaintext metadata (`rating`, `city`, `service`, `created_at`) is not encrypted — it's low-sensitivity and is exactly what an *approved* review shows publicly anyway. A compromised Render account with `DATABASE_URL` access could still see submission volume/timing even with content encrypted; full mitigation would mean not persisting metadata at all, which conflicts with running a working reviews feature.

## Deploy on Render

- Build: `pip install -r requirements.txt`
- Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Runtime: Python 3.12 (pinned in `runtime.txt`)
- To enable reviews: attach a Postgres instance to this service in the Render dashboard (auto-sets `DATABASE_URL`), then add `REVIEWS_ENC_KEY`, `IP_HASH_SALT`, `TELEGRAM_WEBHOOK_SECRET` as env vars, redeploy, then run the `setWebhook` curl above once.
