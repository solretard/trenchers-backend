# Trenchers API

Leaderboard + faction-war backend for the Trenchers game. Built with **FastAPI**.
Runs **in-memory by default** (no database required), and switches to **Supabase**
(Postgres) automatically when you set the env vars.

This is the first multiplayer layer: an **async leaderboard**. Players finish a run,
their score is submitted, and everyone sees the rankings and faction standings.
Live presence / real-time combat are deliberately NOT here yet.

---

## 1. Run it locally (in-memory, zero setup)

```bash
cd trenchers-backend
python -m venv .venv && source .venv/bin/activate    # optional but recommended
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://127.0.0.1:8000/docs for the auto-generated API explorer.

Quick test:

```bash
# submit a run
curl -X POST http://127.0.0.1:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"handle":"Sol_Retard","faction":"SNIPER","scars":840,"depth":3,"kills":52}'

# see the board
curl http://127.0.0.1:8000/leaderboard
curl http://127.0.0.1:8000/factions
```

In-memory data resets when the server restarts. That's expected — it's for testing
the loop before you wire up Supabase.

---

## 2. Add Supabase (persistent storage)

1. Create a project at supabase.com.
2. In the SQL editor, paste and run `schema.sql`.
3. Grab your project URL and the **service role** key (Settings → API).
4. Set env vars (locally in `.env`, or in Railway's Variables tab):

```
SUPABASE_URL=https://YOURPROJECT.supabase.co
SUPABASE_KEY=YOUR_SERVICE_ROLE_KEY
```

Restart. `/health` will now report `"store": "supabase"`.

> **Security:** the service role key bypasses row-level security. It must live ONLY
> on the server. Never put it in the game / browser.

---

## 3. Deploy to Railway

1. Push this folder to a GitHub repo.
2. Railway → New Project → Deploy from repo.
3. Add the env vars from step 2 (plus `ALLOWED_ORIGINS` set to your game's domain).
4. Railway builds with Nixpacks and runs the `Procfile` / `railway.json` start command.
5. You'll get a public URL like `https://trenchers-api.up.railway.app`.

---

## 4. Connect the game

In `trenchers-v3.html`, set the backend URL near the top of the script:

```js
const BACKEND_URL = "https://trenchers-api.up.railway.app";  // empty string = offline
```

When it's set, the game-over screen shows a callsign field + "Submit to leaderboard",
posts the run to `/runs`, and displays the top of the board. When it's empty, the game
stays fully offline (single-file, no network) exactly as before.

---

## Endpoints

| Method | Path           | Purpose                                  |
|--------|----------------|------------------------------------------|
| GET    | `/health`      | status + which store is active           |
| POST   | `/runs`        | submit a finished run, returns your rank  |
| GET    | `/leaderboard` | top players (best run each), `?faction=`  |
| GET    | `/factions`    | faction-war standings                     |

---

## Before real rewards: harden these two hooks

Both are stubs in `main.py`, on purpose:

- **`validate_run()`** — currently a crude plausibility check. The browser sends the
  score, so anyone can fake it. Real protection needs server-authoritative validation
  (e.g. the server issues a signed run token at start, the client returns it, and the
  score is bounded by replayable inputs). Do this before any tokens/NFTs ride on the board.
- **`verify_wallet()`** — currently returns True. Swap in real XRPL signature
  verification over a server-issued nonce so a score belongs to a wallet and can't be
  sybil-farmed. This is what ties the leaderboard to the on-chain reward distribution.

Until those are hardened, treat the leaderboard as **fun/testing only**, not as the
source of truth for handing out rewards.
