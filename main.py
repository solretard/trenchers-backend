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
import time
from typing import Optional, List, Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")          # use the SERVICE ROLE key (server-only!)
SEASON = os.environ.get("TRENCHERS_SEASON", "0")
TABLE = os.environ.get("TRENCHERS_TABLE", "trenchers_runs")
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)
if USE_SUPABASE:
    # Imported lazily so the app still boots in memory mode without the package.
    from supabase import create_client, Client
    sb: "Client" = create_client(SUPABASE_URL, SUPABASE_KEY)

# In-memory store (used when Supabase isn't configured)
_mem_runs: List[Dict] = []

# The three playable classes double as the faction-war teams for now.
FACTIONS = {"APER", "DIAMOND", "SNIPER"}

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class RunIn(BaseModel):
    handle: str = Field(min_length=1, max_length=24)
    faction: str
    scars: int = Field(ge=0, le=10_000_000)
    depth: int = Field(ge=1, le=100_000)
    kills: int = Field(ge=0, le=10_000_000)
    # Future identity / anti-cheat — accepted but not yet enforced.
    wallet: Optional[str] = None
    signature: Optional[str] = None
    run_token: Optional[str] = None


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
    """TODO: real server-authoritative anti-cheat.
    For now: faction must be valid and the score must be vaguely plausible
    given depth + kills, to reject obviously-faked submissions."""
    if run.faction not in FACTIONS:
        return False
    ceiling = 60 * run.depth + 35 * run.kills + 500
    if run.scars > ceiling:
        return False
    return True


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


@app.post("/runs", response_model=RunOut)
def submit_run(run: RunIn):
    run.faction = run.faction.upper()
    if not validate_run(run):
        raise HTTPException(status_code=400, detail="run failed validation")
    if run.wallet and not verify_wallet(run.wallet, run.signature):
        raise HTTPException(status_code=401, detail="bad wallet signature")

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
