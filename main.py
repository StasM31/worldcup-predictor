from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3
import os
import httpx
import asyncio
from datetime import datetime
from contextlib import contextmanager

app = FastAPI()

DATABASE = "predictor.db"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme123")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# ──────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────
@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            telegram_chat_id TEXT,
            token TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            match_time TEXT NOT NULL,
            home_score INTEGER,
            away_score INTEGER,
            status TEXT DEFAULT 'upcoming'
        );
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            match_id INTEGER NOT NULL,
            home_score INTEGER NOT NULL,
            away_score INTEGER NOT NULL,
            points INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(player_id, match_id),
            FOREIGN KEY(player_id) REFERENCES players(id),
            FOREIGN KEY(match_id) REFERENCES matches(id)
        );
        """)

init_db()

# ──────────────────────────────────────────
# Auth
# ──────────────────────────────────────────
def require_admin(x_admin_token: str = Header(...)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

def get_player_by_token(token: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM players WHERE token=?", (token,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid token")
    return row

# ──────────────────────────────────────────
# Telegram helpers
# ──────────────────────────────────────────
async def send_telegram(chat_id: str, text: str):
    if not BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        except Exception as e:
            print(f"Telegram error: {e}")

async def broadcast_predictions(match_id: int):
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        preds = db.execute("""
            SELECT pl.name, p.home_score, p.away_score, pl.telegram_chat_id
            FROM predictions p JOIN players pl ON p.player_id = pl.id
            WHERE p.match_id=?
        """, (match_id,)).fetchall()
        players = db.execute("SELECT * FROM players").fetchall()

    if not match or not preds:
        return

    lines = [f"⚽ <b>{match['home_team']} vs {match['away_team']}</b>\n🔮 Прогнозы участников:\n"]
    for p in preds:
        lines.append(f"• {p['name']}: {p['home_score']}:{p['away_score']}")

    text = "\n".join(lines)
    tasks = []
    for pl in players:
        if pl["telegram_chat_id"]:
            tasks.append(send_telegram(pl["telegram_chat_id"], text))
    await asyncio.gather(*tasks)

def check_and_broadcast(match_id: int):
    """Check if all players predicted, then broadcast."""
    with get_db() as db:
        total_players = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        total_preds = db.execute(
            "SELECT COUNT(*) FROM predictions WHERE match_id=?", (match_id,)
        ).fetchone()[0]
    if total_players > 0 and total_preds >= total_players:
        asyncio.create_task(broadcast_predictions(match_id))

# ──────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────
def calc_points(pred_h, pred_a, real_h, real_a):
    if pred_h == real_h and pred_a == real_a:
        return 3
    pred_outcome = "H" if pred_h > pred_a else ("A" if pred_h < pred_a else "D")
    real_outcome = "H" if real_h > real_a else ("A" if real_h < real_a else "D")
    if pred_outcome == real_outcome:
        return 1
    return 0

# ──────────────────────────────────────────
# Player endpoints
# ──────────────────────────────────────────
class PredictionIn(BaseModel):
    token: str
    home_score: int
    away_score: int

@app.get("/api/matches")
def list_matches(token: str):
    player = get_player_by_token(token)
    with get_db() as db:
        matches = db.execute("SELECT * FROM matches ORDER BY match_time ASC").fetchall()
        my_preds = db.execute(
            "SELECT match_id, home_score, away_score FROM predictions WHERE player_id=?",
            (player["id"],)
        ).fetchall()
    pred_map = {p["match_id"]: p for p in my_preds}
    result = []
    for m in matches:
        d = dict(m)
        if m["id"] in pred_map:
            d["my_prediction"] = {
                "home": pred_map[m["id"]]["home_score"],
                "away": pred_map[m["id"]]["away_score"]
            }
        else:
            d["my_prediction"] = None
        result.append(d)
    return result

@app.post("/api/predict/{match_id}")
async def predict(match_id: int, body: PredictionIn):
    player = get_player_by_token(body.token)
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not match:
            raise HTTPException(404, "Match not found")
        if match["status"] != "upcoming":
            raise HTTPException(400, "Match already started or finished")
        existing = db.execute(
            "SELECT id FROM predictions WHERE player_id=? AND match_id=?",
            (player["id"], match_id)
        ).fetchone()
        if existing:
            raise HTTPException(400, "You already predicted this match")
        db.execute(
            "INSERT INTO predictions (player_id, match_id, home_score, away_score) VALUES (?,?,?,?)",
            (player["id"], match_id, body.home_score, body.away_score)
        )
    check_and_broadcast(match_id)
    return {"ok": True}

@app.get("/api/leaderboard")
def leaderboard(token: str):
    get_player_by_token(token)
    with get_db() as db:
        rows = db.execute("""
            SELECT pl.name,
                   COALESCE(SUM(p.points),0) as total_points,
                   COUNT(p.id) as predictions_count,
                   SUM(CASE WHEN p.points=3 THEN 1 ELSE 0 END) as exact_hits,
                   SUM(CASE WHEN p.points=1 THEN 1 ELSE 0 END) as outcome_hits
            FROM players pl
            LEFT JOIN predictions p ON pl.id = p.player_id
            GROUP BY pl.id
            ORDER BY total_points DESC
        """).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/match/{match_id}/predictions")
def match_predictions(match_id: int, token: str):
    get_player_by_token(token)
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not match:
            raise HTTPException(404, "Match not found")
        # Only show predictions if match is not upcoming
        if match["status"] == "upcoming":
            # Check if all predicted
            total_players = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
            total_preds = db.execute(
                "SELECT COUNT(*) FROM predictions WHERE match_id=?", (match_id,)
            ).fetchone()[0]
            if total_preds < total_players:
                return {"hidden": True, "reason": f"Ждём прогнозов: {total_preds}/{total_players}"}
        preds = db.execute("""
            SELECT pl.name, p.home_score, p.away_score, p.points
            FROM predictions p JOIN players pl ON p.player_id = pl.id
            WHERE p.match_id=?
            ORDER BY pl.name
        """, (match_id,)).fetchall()
    return {"hidden": False, "predictions": [dict(p) for p in preds], "match": dict(match)}

@app.get("/api/me")
def get_me(token: str):
    player = get_player_by_token(token)
    return {"id": player["id"], "name": player["name"]}

# ──────────────────────────────────────────
# Admin endpoints
# ──────────────────────────────────────────
class PlayerIn(BaseModel):
    name: str
    telegram_chat_id: Optional[str] = None

class MatchIn(BaseModel):
    home_team: str
    away_team: str
    match_time: str  # ISO format

class ResultIn(BaseModel):
    home_score: int
    away_score: int

import secrets

@app.post("/api/admin/players", dependencies=[Depends(require_admin)])
def add_player(body: PlayerIn):
    token = secrets.token_urlsafe(16)
    with get_db() as db:
        try:
            db.execute(
                "INSERT INTO players (name, telegram_chat_id, token) VALUES (?,?,?)",
                (body.name, body.telegram_chat_id, token)
            )
        except sqlite3.IntegrityError:
            raise HTTPException(400, "Player already exists")
    return {"name": body.name, "token": token}

@app.get("/api/admin/players", dependencies=[Depends(require_admin)])
def get_players():
    with get_db() as db:
        rows = db.execute("SELECT id, name, telegram_chat_id, token FROM players").fetchall()
    return [dict(r) for r in rows]

@app.put("/api/admin/players/{player_id}", dependencies=[Depends(require_admin)])
def update_player(player_id: int, body: PlayerIn):
    with get_db() as db:
        db.execute(
            "UPDATE players SET name=?, telegram_chat_id=? WHERE id=?",
            (body.name, body.telegram_chat_id, player_id)
        )
    return {"ok": True}

@app.post("/api/admin/matches", dependencies=[Depends(require_admin)])
def add_match(body: MatchIn):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO matches (home_team, away_team, match_time) VALUES (?,?,?)",
            (body.home_team, body.away_team, body.match_time)
        )
    return {"id": cur.lastrowid}

@app.delete("/api/admin/matches/{match_id}", dependencies=[Depends(require_admin)])
def delete_match(match_id: int):
    with get_db() as db:
        db.execute("DELETE FROM predictions WHERE match_id=?", (match_id,))
        db.execute("DELETE FROM matches WHERE id=?", (match_id,))
    return {"ok": True}

@app.put("/api/admin/matches/{match_id}/status", dependencies=[Depends(require_admin)])
def update_match_status(match_id: int, status: str):
    if status not in ("upcoming", "live", "finished"):
        raise HTTPException(400, "Invalid status")
    with get_db() as db:
        db.execute("UPDATE matches SET status=? WHERE id=?", (status, match_id))
    return {"ok": True}

@app.post("/api/admin/matches/{match_id}/result", dependencies=[Depends(require_admin)])
def set_result(match_id: int, body: ResultIn):
    with get_db() as db:
        db.execute(
            "UPDATE matches SET home_score=?, away_score=?, status='finished' WHERE id=?",
            (body.home_score, body.away_score, match_id)
        )
        preds = db.execute(
            "SELECT id, home_score, away_score FROM predictions WHERE match_id=?",
            (match_id,)
        ).fetchall()
        for p in preds:
            pts = calc_points(p["home_score"], p["away_score"], body.home_score, body.away_score)
            db.execute("UPDATE predictions SET points=? WHERE id=?", (pts, p["id"]))
    return {"ok": True}

@app.get("/api/admin/matches", dependencies=[Depends(require_admin)])
def admin_matches():
    with get_db() as db:
        rows = db.execute("SELECT * FROM matches ORDER BY match_time ASC").fetchall()
        preds = db.execute("""
            SELECT p.match_id, pl.name, p.home_score, p.away_score, p.points
            FROM predictions p JOIN players pl ON p.player_id=pl.id
        """).fetchall()
    pred_map = {}
    for p in preds:
        pred_map.setdefault(p["match_id"], []).append(dict(p))
    result = []
    for m in rows:
        d = dict(m)
        d["predictions"] = pred_map.get(m["id"], [])
        result.append(d)
    return result

# ──────────────────────────────────────────
# Telegram webhook for /start (register chat_id)
# ──────────────────────────────────────────
@app.post("/api/telegram/webhook")
async def telegram_webhook(update: dict):
    msg = update.get("message", {})
    text = msg.get("text", "")
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if text.startswith("/start "):
        token = text.split(" ", 1)[1].strip()
        with get_db() as db:
            player = db.execute("SELECT * FROM players WHERE token=?", (token,)).fetchone()
            if player:
                db.execute("UPDATE players SET telegram_chat_id=? WHERE token=?", (chat_id, token))
                await send_telegram(chat_id, f"✅ Привет, {player['name']}! Ты подключён к турниру прогнозов. Теперь будешь получать уведомления перед матчами.")
            else:
                await send_telegram(chat_id, "❌ Неверный токен. Проверь ссылку.")
    return {"ok": True}

# Static files
app.mount("/", StaticFiles(directory="static", html=True), name="static")
