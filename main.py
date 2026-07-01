"""
Trenchers API — leaderboard + faction-war backend.

Runs in two modes:
  * In-memory (default) — no database needed, great for local dev/testing.
  * Supabase — set SUPABASE_URL + SUPABASE_KEY and it persists to Postgres.

Deploy target: Railway (see Procfile + README).
Identity: handle-based for now; wallet-signature hook is stubbed in verify_wallet().
Anti-cheat: validate_run() is a deliberate placeholder — harden before real rewards.
"""

import os
import re
import time
import secrets
from collections import defaultdict
from typing import Optional, List, Dict

import httpx                      # outbound calls to the Xaman platform
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")          # use the SERVICE ROLE key (server-only!)
SEASON = os.environ.get("TRENCHERS_SEASON", "0")
TABLE = os.environ.get("TRENCHERS_TABLE", "trenchers_runs")
PLAYERS_TABLE = os.environ.get("TRENCHERS_PLAYERS_TABLE", "trenchers_players")
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

# Xaman (XUMM) — sign-in via the user's Xaman app. Secret stays server-side.
XAMAN_API_KEY = os.environ.get("XAMAN_API_KEY")
XAMAN_API_SECRET = os.environ.get("XAMAN_API_SECRET")
XAMAN_ENABLED = bool(XAMAN_API_KEY and XAMAN_API_SECRET)
XAMAN_BASE = "https://xumm.app/api/v1/platform"

USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)
if USE_SUPABASE:
    # Imported lazily so the app still boots in memory mode without the package.
    from supabase import create_client, Client
    sb: "Client" = create_client(SUPABASE_URL, SUPABASE_KEY)

# In-memory store (used when Supabase isn't configured)
_mem_runs: List[Dict] = []
_mem_players: Dict[str, Dict] = {}          # wallet -> {"wallet","data","updated_at"}

_WALLET_RE = re.compile(r"^r[1-9A-HJ-NP-Za-km-z]{24,34}$")


def valid_wallet(w: Optional[str]) -> bool:
    return bool(w) and isinstance(w, str) and bool(_WALLET_RE.match(w))

# The three playable classes double as the faction-war teams for now.
FACTIONS = {"APER", "DIAMOND", "SNIPER"}

# --------------------------------------------------------------------------- #
# Anti-cheat tunables (raise the cost of faking; not bulletproof by design)
# --------------------------------------------------------------------------- #
MAX_SCARS_PER_SEC = 500        # a run can't earn more scars/sec than this
MIN_SEC_PER_DEPTH = 3.0        # each room takes at least this long to clear
MIN_SESSION_SEC = 8.0          # a real run lasts at least this long
SESSION_TTL_SEC = 3 * 3600     # a session token is submittable for this long
SESSION_START_COOLDOWN = 6.0   # min seconds between a subject's session starts
MAX_SUBMITS_PER_HOUR = 40      # per subject (wallet/handle) and per IP

# Ephemeral server-side sessions: {sid: {"sub": str, "iat": float, "ip": str, "used": bool}}
_sessions: Dict[str, Dict] = {}
_last_start: Dict[str, float] = {}                 # subject -> last session-start time
_submit_log: Dict[str, List[float]] = defaultdict(list)   # key -> recent submit timestamps


def _client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return req.client.host if req.client else "unknown"


def _rate_ok(key: str) -> bool:
    now = time.time()
    log = [t for t in _submit_log[key] if now - t < 3600]
    _submit_log[key] = log
    return len(log) < MAX_SUBMITS_PER_HOUR


def _rate_hit(key: str):
    _submit_log[key].append(time.time())


def _prune_sessions():
    now = time.time()
    dead = [s for s, v in _sessions.items() if now - v["iat"] > SESSION_TTL_SEC]
    for s in dead:
        _sessions.pop(s, None)

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class RunIn(BaseModel):
    handle: str = Field(min_length=1, max_length=24)
    faction: str
    scars: int = Field(ge=0, le=10_000_000)
    depth: int = Field(ge=1, le=100_000)
    kills: int = Field(ge=0, le=10_000_000)
    session: str = Field(min_length=8, max_length=64)   # required — issued by /session/start
    # Future identity / anti-cheat — accepted but not yet enforced.
    wallet: Optional[str] = None
    signature: Optional[str] = None


class SessionIn(BaseModel):
    handle: Optional[str] = Field(default=None, max_length=24)
    wallet: Optional[str] = None


class SessionOut(BaseModel):
    session: str
    issued_at: int


class PlayerSaveIn(BaseModel):
    wallet: str
    data: Dict = Field(default_factory=dict)


class PlayerOut(BaseModel):
    wallet: str
    data: Dict


class RunOut(BaseModel):
    ok: bool
    rank: Optional[int] = None
    best: Optional[int] = None
    season: str


class LeaderRow(BaseModel):
    rank: int
    handle: str
    faction: str
    scars: int
    depth: int


class FactionRow(BaseModel):
    faction: str
    total_scars: int
    players: int
    top_scars: int


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = FastAPI(title="Trenchers API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Hooks to harden later
# --------------------------------------------------------------------------- #
def verify_wallet(wallet: Optional[str], signature: Optional[str]) -> bool:
    """TODO: verify an XRPL signature over a server-issued nonce.
    Returns True for now so the handle-only flow works during development."""
    return True


def validate_run(run: RunIn) -> bool:
    """Structural plausibility: faction valid and score not exceeding what
    depth + kills could yield. The *timing* gate (in submit_run) is the real
    teeth — this just rejects obviously-inconsistent numbers."""
    if run.faction not in FACTIONS:
        return False
    ceiling = 60 * run.depth + 35 * run.kills + 500
    if run.scars > ceiling:
        return False
    return True


def min_seconds_for(scars: int, depth: int) -> float:
    """Least wall-clock time a legit run of this size could take."""
    return max(MIN_SESSION_SEC, scars / MAX_SCARS_PER_SEC, depth * MIN_SEC_PER_DEPTH)


def sanitize_handle(h: str) -> str:
    cleaned = "".join(c for c in h if c.isalnum() or c in "_-").strip()
    return (cleaned or "anon")[:24]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/")
def root():
    return {"name": "Trenchers API", "season": SEASON, "ok": True}


@app.get("/health")
def health():
    return {
        "ok": True,
        "season": SEASON,
        "store": "supabase" if USE_SUPABASE else "memory",
    }


@app.post("/session/start", response_model=SessionOut)
def session_start(body: SessionIn, request: Request):
    _prune_sessions()
    ip = _client_ip(request)
    sub = (body.wallet or sanitize_handle(body.handle or "") or ip)
    now = time.time()

    # simple per-subject cooldown so you can't spin up sessions in a tight loop
    last = _last_start.get(sub, 0.0)
    if now - last < SESSION_START_COOLDOWN:
        raise HTTPException(status_code=429, detail="slow down")
    if not _rate_ok("ip:" + ip):
        raise HTTPException(status_code=429, detail="too many sessions from this network")
    _last_start[sub] = now

    sid = secrets.token_urlsafe(24)
    _sessions[sid] = {"sub": sub, "iat": now, "ip": ip, "used": False}
    return SessionOut(session=sid, issued_at=int(now))


@app.post("/runs", response_model=RunOut)
def submit_run(run: RunIn, request: Request):
    run.faction = run.faction.upper()
    ip = _client_ip(request)

    # 1) must present a live, unused session token
    sess = _sessions.get(run.session)
    if not sess:
        raise HTTPException(status_code=401, detail="no active session — start a run first")
    if sess["used"]:
        raise HTTPException(status_code=409, detail="session already submitted")
    now = time.time()
    elapsed = now - sess["iat"]
    if elapsed > SESSION_TTL_SEC:
        _sessions.pop(run.session, None)
        raise HTTPException(status_code=410, detail="session expired")

    # 2) structural plausibility
    if not validate_run(run):
        raise HTTPException(status_code=400, detail="run failed validation")

    # 3) timing gate — the score can't have been earned faster than physically possible
    if elapsed + 1.0 < min_seconds_for(run.scars, run.depth):
        raise HTTPException(status_code=400, detail="run too fast to be real")

    # 4) rate limits (per subject and per network)
    sub = sess["sub"]
    if not _rate_ok("sub:" + sub) or not _rate_ok("ip:" + ip):
        raise HTTPException(status_code=429, detail="rate limit — try again later")

    if run.wallet and not verify_wallet(run.wallet, run.signature):
        raise HTTPException(status_code=401, detail="bad wallet signature")

    # consume the session and count against the rate windows
    sess["used"] = True
    _rate_hit("sub:" + sub)
    _rate_hit("ip:" + ip)

    handle = sanitize_handle(run.handle)
    record = {
        "handle": handle,
        "faction": run.faction,
        "scars": run.scars,
        "depth": run.depth,
        "kills": run.kills,
        "wallet": run.wallet,
        "season": SEASON,
        "created_at": int(time.time()),
    }

    if USE_SUPABASE:
        sb.table(TABLE).insert(record).execute()
        higher = (
            sb.table(TABLE)
            .select("id", count="exact")
            .eq("season", SEASON)
            .gt("scars", run.scars)
            .execute()
        )
        rank = (higher.count or 0) + 1
        best_res = (
            sb.table(TABLE)
            .select("scars")
            .eq("season", SEASON)
            .eq("handle", handle)
            .order("scars", desc=True)
            .limit(1)
            .execute()
        )
        best = best_res.data[0]["scars"] if best_res.data else run.scars
    else:
        _mem_runs.append(record)
        rank = sum(
            1 for r in _mem_runs
            if r["season"] == SEASON and r["scars"] > run.scars
        ) + 1
        best = max(
            (r["scars"] for r in _mem_runs
             if r["season"] == SEASON and r["handle"] == handle),
            default=run.scars,
        )

    return RunOut(ok=True, rank=rank, best=best, season=SEASON)


def _xaman_headers():
    return {"X-API-Key": XAMAN_API_KEY, "X-API-Secret": XAMAN_API_SECRET,
            "Content-Type": "application/json"}


@app.post("/xaman/signin")
def xaman_signin():
    """Create a Xaman SignIn request. Returns a QR image URL + deeplink the
    client shows; the user approves in their Xaman app (no payment, no keys)."""
    if not XAMAN_ENABLED:
        raise HTTPException(status_code=503, detail="Xaman not configured")
    try:
        with httpx.Client(timeout=15) as client:
            r = client.post(XAMAN_BASE + "/payload", headers=_xaman_headers(),
                            json={"txjson": {"TransactionType": "SignIn"}})
        r.raise_for_status()
        d = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Xaman request failed")
    return {
        "uuid": d.get("uuid"),
        "next": (d.get("next") or {}).get("always"),
        "qr": (d.get("refs") or {}).get("qr_png"),
    }


@app.get("/xaman/status/{uuid}")
def xaman_status(uuid: str):
    if not XAMAN_ENABLED:
        raise HTTPException(status_code=503, detail="Xaman not configured")
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(XAMAN_BASE + "/payload/" + uuid, headers=_xaman_headers())
        r.raise_for_status()
        d = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Xaman status failed")
    meta = d.get("meta") or {}
    resp = d.get("response") or {}
    return {
        "resolved": bool(meta.get("resolved")),
        "signed": bool(meta.get("signed")),
        "expired": bool(meta.get("expired")),
        "address": resp.get("account"),
    }


@app.post("/player/save")
def player_save(body: PlayerSaveIn, request: Request):
    """Save a player's data under their wallet address (their account).
    NOTE: ownership isn't verified yet — anyone who knows a wallet address can
    write to it. Fine for non-sensitive game progress; add a signed-nonce check
    before storing anything that matters. Keep the blob small."""
    if not valid_wallet(body.wallet):
        raise HTTPException(status_code=400, detail="invalid wallet address")
    if len(str(body.data)) > 20_000:
        raise HTTPException(status_code=413, detail="data too large")
    ip = _client_ip(request)
    if not _rate_ok("save:" + ip):
        raise HTTPException(status_code=429, detail="slow down")
    _rate_hit("save:" + ip)

    record = {"wallet": body.wallet, "data": body.data,
              "season": SEASON, "updated_at": int(time.time())}
    if USE_SUPABASE:
        sb.table(PLAYERS_TABLE).upsert(record, on_conflict="wallet").execute()
    else:
        _mem_players[body.wallet] = record
    return {"ok": True}


@app.get("/player/{wallet}", response_model=PlayerOut)
def player_get(wallet: str):
    if not valid_wallet(wallet):
        raise HTTPException(status_code=400, detail="invalid wallet address")
    if USE_SUPABASE:
        res = sb.table(PLAYERS_TABLE).select("wallet,data").eq("wallet", wallet).limit(1).execute()
        row = res.data[0] if res.data else None
    else:
        row = _mem_players.get(wallet)
    return PlayerOut(wallet=wallet, data=(row["data"] if row else {}))


@app.get("/leaderboard", response_model=List[LeaderRow])
def leaderboard(limit: int = 20, faction: Optional[str] = None):
    limit = max(1, min(limit, 100))

    if USE_SUPABASE:
        q = sb.table(TABLE).select("handle,faction,scars,depth").eq("season", SEASON)
        if faction:
            q = q.eq("faction", faction.upper())
        # Over-fetch so we can keep only each player's best run after dedupe.
        rows = q.order("scars", desc=True).limit(limit * 5).execute().data
    else:
        rows = [r for r in _mem_runs if r["season"] == SEASON]
        if faction:
            rows = [r for r in rows if r["faction"] == faction.upper()]
        rows = sorted(rows, key=lambda r: r["scars"], reverse=True)

    best_per_handle: Dict[str, Dict] = {}
    for r in rows:
        h = r["handle"]
        if h not in best_per_handle or r["scars"] > best_per_handle[h]["scars"]:
            best_per_handle[h] = r

    top = sorted(best_per_handle.values(), key=lambda r: r["scars"], reverse=True)[:limit]
    return [
        LeaderRow(rank=i + 1, handle=r["handle"], faction=r["faction"],
                  scars=r["scars"], depth=r["depth"])
        for i, r in enumerate(top)
    ]


@app.get("/factions", response_model=List[FactionRow])
def factions():
    """Faction war standings. Each player's BEST run counts once toward their
    faction's total, so it rewards a faction's spread of strong players rather
    than letting one grinder submit a thousand runs."""
    if USE_SUPABASE:
        rows = sb.table(TABLE).select("handle,faction,scars").eq("season", SEASON).execute().data
    else:
        rows = [r for r in _mem_runs if r["season"] == SEASON]

    best_by_handle: Dict[tuple, int] = {}
    for r in rows:
        key = (r["faction"], r["handle"])
        if key not in best_by_handle or r["scars"] > best_by_handle[key]:
            best_by_handle[key] = r["scars"]

    agg: Dict[str, Dict] = {}
    for (fac, _handle), sc in best_by_handle.items():
        a = agg.setdefault(fac, {"total": 0, "players": 0, "top": 0})
        a["total"] += sc
        a["players"] += 1
        a["top"] = max(a["top"], sc)

    out = [
        FactionRow(faction=f, total_scars=v["total"], players=v["players"], top_scars=v["top"])
        for f, v in agg.items()
    ]
    out.sort(key=lambda x: x.total_scars, reverse=True)
    return out
