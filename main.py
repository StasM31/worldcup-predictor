from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
import sqlite3, os, httpx, asyncio, secrets
from contextlib import contextmanager
from datetime import datetime, timezone

app = FastAPI()

DATABASE = "predictor.db"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme123")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GRACE_SECONDS = 60  # 1 минута грейс-период

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
            started_at TEXT,
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

# ── Авто-старт матчей ──
async def auto_start_scheduler():
    """Каждые 30 секунд проверяет — не пора ли стартовать матч."""
    await asyncio.sleep(5)
    while True:
        try:
            now = datetime.now(timezone.utc)
            with get_db() as db:
                matches = db.execute("SELECT * FROM matches WHERE status='upcoming'").fetchall()
                for m in matches:
                    mt_str = m["match_time"]
                    mt = None
                    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                        try:
                            mt = datetime.strptime(mt_str, fmt).replace(tzinfo=timezone.utc)
                            break
                        except ValueError:
                            continue
                    if mt and now >= mt:
                        db.execute("UPDATE matches SET status='grace', started_at=? WHERE id=?",
                                   (now.isoformat(), m["id"]))
                        asyncio.create_task(grace_period_end(m["id"]))
                        print(f"Auto-started: {m['home_team']} vs {m['away_team']}")
        except Exception as e:
            print(f"Scheduler error: {e}")
        await asyncio.sleep(30)

@app.on_event("startup")
async def startup():
    asyncio.create_task(auto_start_scheduler())

def require_admin(x_admin_token: str = Header(...)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

def get_player_by_token(token: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM players WHERE token=?", (token,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Неверный токен")
    return row

# ── Telegram ──
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
            SELECT pl.name, p.home_score, p.away_score
            FROM predictions p JOIN players pl ON p.player_id = pl.id
            WHERE p.match_id=?
        """, (match_id,)).fetchall()
        all_players = db.execute("SELECT id, name, telegram_chat_id FROM players").fetchall()

    if not match:
        return

    pred_player_ids = set()
    with get_db() as db:
        rows = db.execute("SELECT player_id FROM predictions WHERE match_id=?", (match_id,)).fetchall()
        pred_player_ids = {r["player_id"] for r in rows}

    lines = [f"⚽ <b>{match['home_team']} vs {match['away_team']}</b>\n🔮 Прогнозы на матч:\n"]
    for pl in all_players:
        if pl["id"] in pred_player_ids:
            p = next(x for x in preds if x["name"] == pl["name"])
            lines.append(f"• {pl['name']}: <b>{p['home_score']}:{p['away_score']}</b>")
        else:
            lines.append(f"• {pl['name']}: 😴 нет прогноза")

    text = "\n".join(lines)
    tasks = [send_telegram(str(pl["telegram_chat_id"]), text) for pl in all_players if pl["telegram_chat_id"]]
    if tasks:
        await asyncio.gather(*tasks)

def check_and_broadcast(match_id: int):
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        done = db.execute("SELECT COUNT(*) FROM predictions WHERE match_id=?", (match_id,)).fetchone()[0]
    if total > 0 and done >= total:
        asyncio.create_task(broadcast_predictions(match_id))

async def grace_period_end(match_id: int):
    """Called after grace period — close betting and broadcast."""
    await asyncio.sleep(GRACE_SECONDS)
    with get_db() as db:
        match = db.execute("SELECT status FROM matches WHERE id=?", (match_id,)).fetchone()
        if match and match["status"] == "grace":
            db.execute("UPDATE matches SET status='live' WHERE id=?", (match_id,))
    await broadcast_predictions(match_id)

def calc_points(ph, pa, rh, ra):
    if ph == rh and pa == ra:
        return 3
    po = "H" if ph > pa else ("A" if ph < pa else "D")
    ro = "H" if rh > ra else ("A" if rh < ra else "D")
    return 1 if po == ro else 0

# ── Player endpoints ──
class PredictionIn(BaseModel):
    token: str
    home_score: int
    away_score: int

@app.get("/api/me")
def get_me(token: str):
    player = get_player_by_token(token)
    return {"id": player["id"], "name": player["name"]}

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
        d["my_prediction"] = {"home": pred_map[m["id"]]["home_score"], "away": pred_map[m["id"]]["away_score"]} if m["id"] in pred_map else None
        result.append(d)
    return result

@app.post("/api/predict/{match_id}")
async def predict(match_id: int, body: PredictionIn):
    player = get_player_by_token(body.token)
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not match:
            raise HTTPException(404, "Матч не найден")
        if match["status"] not in ("upcoming", "grace"):
            raise HTTPException(400, "Приём ставок закрыт")
        if db.execute("SELECT id FROM predictions WHERE player_id=? AND match_id=?", (player["id"], match_id)).fetchone():
            raise HTTPException(400, "Ты уже сделал прогноз")
        db.execute(
            "INSERT INTO predictions (player_id, match_id, home_score, away_score) VALUES (?,?,?,?)",
            (player["id"], match_id, body.home_score, body.away_score)
        )
    check_and_broadcast(match_id)
    return {"ok": True}

@app.get("/api/match/{match_id}/predictions")
def match_predictions(match_id: int, token: str):
    get_player_by_token(token)
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not match:
            raise HTTPException(404)
        if match["status"] == "upcoming":
            total = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
            done = db.execute("SELECT COUNT(*) FROM predictions WHERE match_id=?", (match_id,)).fetchone()[0]
            if done < total:
                return {"hidden": True, "reason": f"Ждём прогнозов: {done}/{total}"}
        preds = db.execute("""
            SELECT pl.name, p.home_score, p.away_score, p.points
            FROM predictions p JOIN players pl ON p.player_id=pl.id
            WHERE p.match_id=? ORDER BY pl.name
        """, (match_id,)).fetchall()
    return {"hidden": False, "predictions": [dict(p) for p in preds], "match": dict(match)}

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
            LEFT JOIN predictions p ON pl.id=p.player_id
            GROUP BY pl.id ORDER BY total_points DESC
        """).fetchall()
    return [dict(r) for r in rows]

# ── Admin endpoints ──
class PlayerIn(BaseModel):
    name: str
    telegram_chat_id: Optional[str] = None

class MatchIn(BaseModel):
    home_team: str
    away_team: str
    match_time: str

class ResultIn(BaseModel):
    home_score: int
    away_score: int

@app.post("/api/admin/players", dependencies=[Depends(require_admin)])
def add_player(body: PlayerIn):
    token = secrets.token_urlsafe(16)
    with get_db() as db:
        try:
            db.execute("INSERT INTO players (name, telegram_chat_id, token) VALUES (?,?,?)",
                       (body.name, body.telegram_chat_id, token))
        except sqlite3.IntegrityError:
            raise HTTPException(400, "Участник уже существует")
    return {"name": body.name, "token": token}

@app.get("/api/admin/players", dependencies=[Depends(require_admin)])
def get_players():
    with get_db() as db:
        rows = db.execute("SELECT id, name, telegram_chat_id, token FROM players").fetchall()
    return [dict(r) for r in rows]

@app.put("/api/admin/players/{player_id}", dependencies=[Depends(require_admin)])
def update_player(player_id: int, body: PlayerIn):
    with get_db() as db:
        db.execute("UPDATE players SET name=?, telegram_chat_id=? WHERE id=?",
                   (body.name, body.telegram_chat_id, player_id))
    return {"ok": True}

@app.post("/api/admin/matches", dependencies=[Depends(require_admin)])
def add_match(body: MatchIn):
    with get_db() as db:
        cur = db.execute("INSERT INTO matches (home_team, away_team, match_time) VALUES (?,?,?)",
                         (body.home_team, body.away_team, body.match_time))
    return {"id": cur.lastrowid}

@app.delete("/api/admin/matches/{match_id}", dependencies=[Depends(require_admin)])
def delete_match(match_id: int):
    with get_db() as db:
        db.execute("DELETE FROM predictions WHERE match_id=?", (match_id,))
        db.execute("DELETE FROM matches WHERE id=?", (match_id,))
    return {"ok": True}

@app.post("/api/admin/matches/{match_id}/start", dependencies=[Depends(require_admin)])
async def start_match(match_id: int):
    """Start grace period — 1 minute window to still place bets."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not match:
            raise HTTPException(404)
        db.execute("UPDATE matches SET status='grace', started_at=? WHERE id=?", (now, match_id))
    asyncio.create_task(grace_period_end(match_id))
    return {"ok": True}

@app.put("/api/admin/matches/{match_id}/status", dependencies=[Depends(require_admin)])
def update_match_status(match_id: int, status: str):
    if status not in ("upcoming", "live", "grace", "finished"):
        raise HTTPException(400, "Invalid status")
    with get_db() as db:
        db.execute("UPDATE matches SET status=? WHERE id=?", (status, match_id))
    return {"ok": True}

@app.post("/api/admin/matches/{match_id}/result", dependencies=[Depends(require_admin)])
def set_result(match_id: int, body: ResultIn):
    with get_db() as db:
        db.execute("UPDATE matches SET home_score=?, away_score=?, status='finished' WHERE id=?",
                   (body.home_score, body.away_score, match_id))
        preds = db.execute("SELECT id, home_score, away_score FROM predictions WHERE match_id=?",
                           (match_id,)).fetchall()
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

# ── Telegram webhook ──
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
                await send_telegram(chat_id, f"✅ Привет, <b>{player['name']}</b>! Ты подключён к турниру прогнозов ⚽\n\nТеперь ты будешь получать уведомления с прогнозами перед каждым матчем. Удачи! 🏆")
            else:
                await send_telegram(chat_id, "❌ Неверный токен. Проверь ссылку.")
    return {"ok": True}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
