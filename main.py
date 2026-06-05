from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
import sqlite3, os, httpx, asyncio, secrets
from contextlib import contextmanager
from datetime import datetime, timezone

app = FastAPI()

# Храним базу в /app/data/ если папка существует (Railway Volume), иначе рядом с кодом
_DATA_DIR = "/app/data" if os.path.isdir("/app/data") else "."
os.makedirs(_DATA_DIR, exist_ok=True)
DATABASE = os.path.join(_DATA_DIR, "predictor.db")

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme123")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BACKUP_ADMIN_CHAT_ID = os.environ.get("BACKUP_CHAT_ID", "")
GRACE_SECONDS = 60
MATCH_DURATION_SECONDS = 120 * 60

@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
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
            is_vabank INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(player_id, match_id),
            FOREIGN KEY(player_id) REFERENCES players(id),
            FOREIGN KEY(match_id) REFERENCES matches(id)
        );
        CREATE TABLE IF NOT EXISTS vabank_used (
            player_id INTEGER PRIMARY KEY,
            FOREIGN KEY(player_id) REFERENCES players(id)
        );
        CREATE TABLE IF NOT EXISTS tournament_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER UNIQUE NOT NULL,
            champion TEXT, finalist1 TEXT, finalist2 TEXT, top_scorer TEXT,
            champion_pts INTEGER DEFAULT 0, finalist_pts INTEGER DEFAULT 0, scorer_pts INTEGER DEFAULT 0,
            FOREIGN KEY(player_id) REFERENCES players(id)
        );
        CREATE TABLE IF NOT EXISTS tournament_result (
            id INTEGER PRIMARY KEY CHECK (id=1),
            champion TEXT, finalist1 TEXT, finalist2 TEXT, top_scorer TEXT
        );
        """)
        try:
            db.execute("ALTER TABLE predictions ADD COLUMN is_vabank INTEGER DEFAULT 0")
        except:
            pass

init_db()

def parse_dt(s):
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

def require_admin(x_admin_token: str = Header(...)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Forbidden")

def get_player_by_token(token: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM players WHERE token=?", (token,)).fetchone()
    if not row:
        raise HTTPException(401, "Неверный токен")
    return row

# ── Флаги ──
TEAM_FLAGS = {
    'россия':'RU','германия':'DE','франция':'FR','испания':'ES','италия':'IT',
    'бразилия':'BR','аргентина':'AR','португалия':'PT','нидерланды':'NL','голландия':'NL',
    'англия':'GB','великобритания':'GB','бельгия':'BE','хорватия':'HR','дания':'DK',
    'швейцария':'CH','польша':'PL','швеция':'SE','норвегия':'NO','австрия':'AT',
    'чехия':'CZ','венгрия':'HU','румыния':'RO','сербия':'RS','греция':'GR',
    'турция':'TR','украина':'UA','сша':'US','мексика':'MX','канада':'CA',
    'япония':'JP','южная корея':'KR','корея':'KR','австралия':'AU','иран':'IR',
    'саудовская аравия':'SA','марокко':'MA','сенегал':'SN','гана':'GH','камерун':'CM',
    'нигерия':'NG','египет':'EG','тунис':'TN','эквадор':'EC','уругвай':'UY',
    'колумбия':'CO','чили':'CL','перу':'PE','катар':'QA','ирак':'IQ',
    'израиль':'IL','словакия':'SK','финляндия':'FI','ирландия':'IE',
    'алжир':'DZ','иордания':'JO','узбекистан':'UZ','гаити':'HT',
    'кюрасао':'CW','кабо-верде':'CV','юар':'ZA','парагвай':'PY','армения':'AM',
    'панама':'PA','венесуэла':'VE','новая зеландия':'NZ',
    'босния и герцеговина':'BA','др конго':'CD',
}

def get_flag_emoji(name):
    if not name: return ''
    key = name.strip().lower()
    code = TEAM_FLAGS.get(key,'')
    if not code:
        for k,v in TEAM_FLAGS.items():
            if key in k or k in key:
                code = v; break
    if not code: return ''
    return ''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in code.upper())

def team_with_flag(name):
    f = get_flag_emoji(name)
    return f"{name} {f}" if f else name

async def send_telegram(chat_id, text):
    if not BOT_TOKEN or not chat_id: return
    async with httpx.AsyncClient() as client:
        try:
            await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                              json={"chat_id":chat_id,"text":text,"parse_mode":"HTML"})
        except Exception as e:
            print(f"TG error: {e}")

async def broadcast_predictions(match_id):
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        preds = db.execute("""SELECT pl.name,p.home_score,p.away_score,p.is_vabank
            FROM predictions p JOIN players pl ON p.player_id=pl.id WHERE p.match_id=?""", (match_id,)).fetchall()
        all_players = db.execute("SELECT id,name,telegram_chat_id FROM players").fetchall()
    if not match: return
    pred_map = {p["name"]: p for p in preds}
    home = team_with_flag(match['home_team'])
    away = team_with_flag(match['away_team'])
    lines = [f"⚽ <b>{home} vs {away}</b>\n🔮 Прогнозы:\n"]
    for pl in all_players:
        p = pred_map.get(pl["name"])
        if p:
            vb = " 🔥<b>ВА-БАНК</b>" if p["is_vabank"] else ""
            lines.append(f"• {pl['name']}: <b>{p['home_score']}:{p['away_score']}</b>{vb}")
        else:
            lines.append(f"• {pl['name']}: 😴 нет прогноза")
    text = "\n".join(lines)
    tasks = [send_telegram(str(pl["telegram_chat_id"]), text) for pl in all_players if pl["telegram_chat_id"]]
    if tasks: await asyncio.gather(*tasks)

def check_and_broadcast(match_id):
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        done = db.execute("SELECT COUNT(*) FROM predictions WHERE match_id=?", (match_id,)).fetchone()[0]
    if total > 0 and done >= total:
        asyncio.create_task(broadcast_predictions(match_id))

async def grace_period_end(match_id):
    await asyncio.sleep(GRACE_SECONDS)
    with get_db() as db:
        m = db.execute("SELECT status FROM matches WHERE id=?", (match_id,)).fetchone()
        if m and m["status"] == "grace":
            db.execute("UPDATE matches SET status='live' WHERE id=?", (match_id,))
    await broadcast_predictions(match_id)
    asyncio.create_task(auto_finish_match(match_id))

async def auto_finish_match(match_id):
    await asyncio.sleep(MATCH_DURATION_SECONDS - GRACE_SECONDS)
    with get_db() as db:
        m = db.execute("SELECT status FROM matches WHERE id=?", (match_id,)).fetchone()
        if m and m["status"] == "live":
            db.execute("UPDATE matches SET status='ended' WHERE id=?", (match_id,))

def calc_points(ph, pa, rh, ra, is_vabank=False):
    if ph==rh and pa==ra: return 9 if is_vabank else 3
    po = "H" if ph>pa else ("A" if ph<pa else "D")
    ro = "H" if rh>ra else ("A" if rh<ra else "D")
    if po==ro: return 3 if is_vabank else 1
    return 0

# ── Schedulers ──
async def auto_start_scheduler():
    from datetime import timedelta
    await asyncio.sleep(5)
    while True:
        try:
            now_msk = (datetime.now(timezone.utc)+timedelta(hours=3)).replace(tzinfo=None)
            with get_db() as db:
                matches = db.execute("SELECT * FROM matches WHERE status='upcoming'").fetchall()
                for m in matches:
                    mt = parse_dt(m["match_time"])
                    if mt and now_msk >= mt.replace(tzinfo=None):
                        db.execute("UPDATE matches SET status='grace', started_at=? WHERE id=?",
                                  (datetime.now(timezone.utc).isoformat(), m["id"]))
                        asyncio.create_task(grace_period_end(m["id"]))
        except Exception as e:
            print(f"Scheduler error: {e}")
        await asyncio.sleep(30)

async def daily_digest_scheduler():
    from datetime import timedelta
    await asyncio.sleep(10)
    last_sent = None
    while True:
        try:
            now_msk = (datetime.now(timezone.utc)+timedelta(hours=3)).replace(tzinfo=None)
            today = now_msk.date()
            if now_msk.hour==18 and now_msk.minute<1 and last_sent!=today:
                last_sent = today
                await send_daily_digest()
        except Exception as e:
            print(f"Digest error: {e}")
        await asyncio.sleep(30)

async def send_daily_digest():
    from datetime import timedelta
    with get_db() as db:
        rows = db.execute("""
            SELECT pl.name,
                   COALESCE(SUM(p.points),0)+COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as total_points,
                   SUM(CASE WHEN p.points>=3 THEN 1 ELSE 0 END) as exact_hits,
                   SUM(CASE WHEN p.points=1 THEN 1 ELSE 0 END) as outcome_hits
            FROM players pl LEFT JOIN predictions p ON pl.id=p.player_id
            GROUP BY pl.id ORDER BY total_points DESC""").fetchall()
        players = db.execute("SELECT telegram_chat_id FROM players WHERE telegram_chat_id IS NOT NULL").fetchall()
    if not rows: return
    medals = ['🥇','🥈','🥉']
    today_msk = (datetime.now(timezone.utc)+timedelta(hours=3)).strftime('%d.%m.%Y')
    lines = [f"📊 <b>Таблица лидеров на {today_msk}</b>\n"]
    for i,r in enumerate(rows):
        medal = medals[i] if i<3 else f"{i+1}."
        lines.append(f"{medal} <b>{r['name']}</b> — <b>{r['total_points']} очк.</b>  🎯{r['exact_hits'] or 0} ✅{r['outcome_hits'] or 0}")
    text = "\n".join(lines)
    tasks = [send_telegram(str(pl["telegram_chat_id"]), text) for pl in players]
    if tasks: await asyncio.gather(*tasks)

async def daily_backup_scheduler():
    from datetime import timedelta
    await asyncio.sleep(15)
    last_backup = None
    while True:
        try:
            now_msk = (datetime.now(timezone.utc)+timedelta(hours=3)).replace(tzinfo=None)
            today = now_msk.date()
            if now_msk.hour==3 and now_msk.minute<1 and last_backup!=today:
                last_backup = today
                await send_backup()
        except Exception as e:
            print(f"Backup error: {e}")
        await asyncio.sleep(30)

async def send_backup():
    chat_id = BACKUP_ADMIN_CHAT_ID
    if not BOT_TOKEN or not chat_id: return
    if not os.path.exists(DATABASE): return
    try:
        from datetime import timedelta
        now_msk = (datetime.now(timezone.utc)+timedelta(hours=3)).strftime("%d.%m.%Y_%H-%M")
        async with httpx.AsyncClient() as client:
            with open(DATABASE, "rb") as f:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                    data={"chat_id":chat_id,"caption":f"🗄 Бэкап базы данных\n📅 {now_msk} МСК"},
                    files={"document":(f"backup_{now_msk}.db",f,"application/octet-stream")},
                    timeout=30)
    except Exception as e:
        print(f"Backup error: {e}")

@app.on_event("startup")
async def startup():
    asyncio.create_task(auto_start_scheduler())
    asyncio.create_task(daily_digest_scheduler())
    asyncio.create_task(daily_backup_scheduler())

@app.post("/api/admin/send-digest", dependencies=[Depends(require_admin)])
async def manual_digest():
    await send_daily_digest(); return {"ok":True}

@app.post("/api/admin/send-backup", dependencies=[Depends(require_admin)])
async def manual_backup():
    await send_backup(); return {"ok":True}

# ── Player endpoints ──
class PredictionIn(BaseModel):
    token: str; home_score: int; away_score: int; is_vabank: bool = False

@app.get("/api/me")
def get_me(token: str):
    player = get_player_by_token(token)
    with get_db() as db:
        vb = db.execute("SELECT 1 FROM vabank_used WHERE player_id=?", (player["id"],)).fetchone()
    return {"id":player["id"],"name":player["name"],"vabank_used":bool(vb)}

@app.get("/api/matches")
def list_matches(token: str):
    player = get_player_by_token(token)
    with get_db() as db:
        matches = db.execute("SELECT * FROM matches ORDER BY match_time ASC").fetchall()
        my_preds = db.execute("SELECT match_id,home_score,away_score,is_vabank FROM predictions WHERE player_id=?", (player["id"],)).fetchall()
    pred_map = {p["match_id"]: dict(p) for p in my_preds}
    result = []
    for m in matches:
        d = dict(m); d["my_prediction"] = pred_map.get(m["id"])
        result.append(d)
    return result

@app.post("/api/predict/{match_id}")
async def predict(match_id: int, body: PredictionIn):
    player = get_player_by_token(body.token)
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not match: raise HTTPException(404,"Матч не найден")
        if match["status"] not in ("upcoming","grace"): raise HTTPException(400,"Приём ставок закрыт")
        existing = db.execute("SELECT id,is_vabank FROM predictions WHERE player_id=? AND match_id=?", (player["id"],match_id)).fetchone()
        if existing and match["status"]=="grace": raise HTTPException(400,"Матч уже начался")
        is_vabank = 0
        if body.is_vabank:
            if not existing:
                if db.execute("SELECT 1 FROM vabank_used WHERE player_id=?", (player["id"],)).fetchone():
                    raise HTTPException(400,"Ва-банк уже использован")
                db.execute("INSERT OR IGNORE INTO vabank_used (player_id) VALUES (?)", (player["id"],))
            is_vabank = 1
        if existing:
            if existing["is_vabank"] and not is_vabank:
                db.execute("DELETE FROM vabank_used WHERE player_id=?", (player["id"],))
            db.execute("UPDATE predictions SET home_score=?,away_score=?,is_vabank=? WHERE player_id=? AND match_id=?",
                      (body.home_score,body.away_score,is_vabank,player["id"],match_id))
        else:
            db.execute("INSERT INTO predictions (player_id,match_id,home_score,away_score,is_vabank) VALUES (?,?,?,?,?)",
                      (player["id"],match_id,body.home_score,body.away_score,is_vabank))
    check_and_broadcast(match_id)
    return {"ok":True}

@app.get("/api/match/{match_id}/predictions")
def match_predictions(match_id: int, token: str):
    get_player_by_token(token)
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not match: raise HTTPException(404)
        total = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        done = db.execute("SELECT COUNT(*) FROM predictions WHERE match_id=?", (match_id,)).fetchone()[0]
        if match["status"]=="upcoming":
            return {"hidden":True,"reason":f"Прогнозы скрыты до начала матча • {done}/{total} сделали ставку"}
        preds = db.execute("""SELECT pl.name,p.home_score,p.away_score,p.points,p.is_vabank
            FROM predictions p JOIN players pl ON p.player_id=pl.id
            WHERE p.match_id=? ORDER BY pl.name""", (match_id,)).fetchall()
    return {"hidden":False,"predictions":[dict(p) for p in preds],"match":dict(match)}

@app.get("/api/leaderboard")
def leaderboard(token: str):
    get_player_by_token(token)
    with get_db() as db:
        rows = db.execute("""
            SELECT pl.name,
                   COALESCE(SUM(p.points),0)+COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as total_points,
                   COUNT(p.id) as predictions_count,
                   SUM(CASE WHEN p.points>=3 THEN 1 ELSE 0 END) as exact_hits,
                   SUM(CASE WHEN p.points=1 THEN 1 ELSE 0 END) as outcome_hits,
                   CASE WHEN COUNT(p.id)>0 THEN ROUND(100.0*SUM(CASE WHEN p.points>0 THEN 1 ELSE 0 END)/COUNT(p.id),1) ELSE 0 END as hit_pct,
                   COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as tournament_bonus
            FROM players pl LEFT JOIN predictions p ON pl.id=p.player_id
            GROUP BY pl.id ORDER BY total_points DESC""").fetchall()
    return [dict(r) for r in rows]

@app.get("/api/archive")
def archive(token: str):
    get_player_by_token(token)
    with get_db() as db:
        matches = db.execute("SELECT * FROM matches WHERE status='finished' ORDER BY match_time DESC").fetchall()
        result = []
        for m in matches:
            preds = db.execute("""SELECT pl.name,p.home_score,p.away_score,p.points,p.is_vabank
                FROM predictions p JOIN players pl ON p.player_id=pl.id
                WHERE p.match_id=? ORDER BY p.points DESC,pl.name""", (m["id"],)).fetchall()
            d = dict(m); d["predictions"] = [dict(p) for p in preds]
            result.append(d)
    return result

class TournamentPredIn(BaseModel):
    token: str; champion: str; finalist1: str; finalist2: str; top_scorer: str

@app.post("/api/tournament-prediction")
def set_tournament_prediction(body: TournamentPredIn):
    player = get_player_by_token(body.token)
    with get_db() as db:
        db.execute("""INSERT INTO tournament_predictions (player_id,champion,finalist1,finalist2,top_scorer)
            VALUES (?,?,?,?,?)
            ON CONFLICT(player_id) DO UPDATE SET
              champion=excluded.champion,finalist1=excluded.finalist1,
              finalist2=excluded.finalist2,top_scorer=excluded.top_scorer""",
            (player["id"],body.champion,body.finalist1,body.finalist2,body.top_scorer))
    return {"ok":True}

@app.get("/api/tournament-prediction")
def get_tournament_prediction(token: str):
    player = get_player_by_token(token)
    with get_db() as db:
        row = db.execute("SELECT * FROM tournament_predictions WHERE player_id=?", (player["id"],)).fetchone()
        result = db.execute("SELECT * FROM tournament_result WHERE id=1").fetchone()
    return {"my_prediction":dict(row) if row else None,"result":dict(result) if result else None}

@app.get("/api/tournament-predictions-all")
def get_all_tournament_predictions(token: str):
    player = get_player_by_token(token)
    with get_db() as db:
        started = db.execute("SELECT COUNT(*) FROM matches WHERE status != 'upcoming'").fetchone()[0] > 0
        total_players = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        done_players = db.execute("SELECT COUNT(*) FROM tournament_predictions").fetchone()[0]
        rows = db.execute("""SELECT pl.name,tp.champion,tp.finalist1,tp.finalist2,tp.top_scorer,
                   tp.champion_pts,tp.finalist_pts,tp.scorer_pts,pl.id as player_id
            FROM tournament_predictions tp JOIN players pl ON tp.player_id=pl.id""").fetchall()
        result = db.execute("SELECT * FROM tournament_result WHERE id=1").fetchone()
    if started:
        return {"predictions":[dict(r) for r in rows],"result":dict(result) if result else None,
                "tournament_started":True,"done_count":done_players,"total_count":total_players}
    my_pred = next((dict(r) for r in rows if r["player_id"]==player["id"]),None)
    return {"predictions":[my_pred] if my_pred else [],"result":None,
            "tournament_started":False,"done_count":done_players,"total_count":total_players}

# ── Admin ──
class PlayerIn(BaseModel):
    name: str; telegram_chat_id: Optional[str] = None
class MatchIn(BaseModel):
    home_team: str; away_team: str; match_time: str
class MatchBatchIn(BaseModel):
    matches: List[MatchIn]
class ResultIn(BaseModel):
    home_score: int; away_score: int
class TournamentResultIn(BaseModel):
    champion: str; finalist1: str; finalist2: str; top_scorer: str

@app.post("/api/admin/players", dependencies=[Depends(require_admin)])
def add_player(body: PlayerIn):
    token = secrets.token_urlsafe(16)
    with get_db() as db:
        try:
            db.execute("INSERT INTO players (name,telegram_chat_id,token) VALUES (?,?,?)",
                      (body.name,body.telegram_chat_id,token))
        except sqlite3.IntegrityError:
            raise HTTPException(400,"Участник уже существует")
    return {"name":body.name,"token":token}

@app.get("/api/admin/players", dependencies=[Depends(require_admin)])
def get_players():
    with get_db() as db:
        rows = db.execute("SELECT id,name,telegram_chat_id,token FROM players").fetchall()
    return [dict(r) for r in rows]

@app.post("/api/admin/matches", dependencies=[Depends(require_admin)])
def add_match(body: MatchIn):
    with get_db() as db:
        cur = db.execute("INSERT INTO matches (home_team,away_team,match_time) VALUES (?,?,?)",
                        (body.home_team,body.away_team,body.match_time))
    return {"id":cur.lastrowid}

@app.post("/api/admin/matches/batch", dependencies=[Depends(require_admin)])
def add_matches_batch(body: MatchBatchIn):
    added = 0
    with get_db() as db:
        for m in body.matches:
            try:
                db.execute("INSERT INTO matches (home_team,away_team,match_time) VALUES (?,?,?)",
                          (m.home_team,m.away_team,m.match_time))
                added += 1
            except Exception as e:
                print(f"Batch error: {e}")
    return {"added":added}

@app.delete("/api/admin/matches/{match_id}", dependencies=[Depends(require_admin)])
def delete_match(match_id: int):
    with get_db() as db:
        db.execute("DELETE FROM predictions WHERE match_id=?", (match_id,))
        db.execute("DELETE FROM matches WHERE id=?", (match_id,))
    return {"ok":True}

@app.post("/api/admin/matches/{match_id}/result", dependencies=[Depends(require_admin)])
def set_result(match_id: int, body: ResultIn):
    with get_db() as db:
        db.execute("UPDATE matches SET home_score=?,away_score=?,status='finished' WHERE id=?",
                  (body.home_score,body.away_score,match_id))
        preds = db.execute("SELECT id,home_score,away_score,is_vabank FROM predictions WHERE match_id=?", (match_id,)).fetchall()
        for p in preds:
            pts = calc_points(p["home_score"],p["away_score"],body.home_score,body.away_score,bool(p["is_vabank"]))
            db.execute("UPDATE predictions SET points=? WHERE id=?", (pts,p["id"]))
    return {"ok":True}

@app.get("/api/admin/matches", dependencies=[Depends(require_admin)])
def admin_matches():
    with get_db() as db:
        rows = db.execute("SELECT * FROM matches ORDER BY match_time ASC").fetchall()
        preds = db.execute("""SELECT p.match_id,pl.name,p.home_score,p.away_score,p.points,p.is_vabank
            FROM predictions p JOIN players pl ON p.player_id=pl.id""").fetchall()
    pred_map = {}
    for p in preds:
        pred_map.setdefault(p["match_id"],[]).append(dict(p))
    return [dict(m)|{"predictions":pred_map.get(m["id"],[])} for m in rows]

@app.post("/api/admin/tournament-result", dependencies=[Depends(require_admin)])
def set_tournament_result(body: TournamentResultIn):
    with get_db() as db:
        db.execute("""INSERT INTO tournament_result (id,champion,finalist1,finalist2,top_scorer)
            VALUES (1,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET champion=excluded.champion,finalist1=excluded.finalist1,
              finalist2=excluded.finalist2,top_scorer=excluded.top_scorer""",
            (body.champion,body.finalist1,body.finalist2,body.top_scorer))
        preds = db.execute("SELECT * FROM tournament_predictions").fetchall()
        for tp in preds:
            c = 10 if tp["champion"] and tp["champion"].lower().strip()==body.champion.lower().strip() else 0
            fins = {body.finalist1.lower().strip(),body.finalist2.lower().strip()}
            f = 0
            if tp["finalist1"] and tp["finalist1"].lower().strip() in fins: f+=5
            if tp["finalist2"] and tp["finalist2"].lower().strip() in fins: f+=5
            s = 10 if tp["top_scorer"] and tp["top_scorer"].lower().strip()==body.top_scorer.lower().strip() else 0
            db.execute("UPDATE tournament_predictions SET champion_pts=?,finalist_pts=?,scorer_pts=? WHERE player_id=?",
                      (c,f,s,tp["player_id"]))
    return {"ok":True}

@app.post("/api/telegram/webhook")
async def telegram_webhook(update: dict):
    msg = update.get("message",{})
    text = msg.get("text","")
    chat_id = str(msg.get("chat",{}).get("id",""))
    if text.startswith("/start "):
        token = text.split(" ",1)[1].strip()
        with get_db() as db:
            player = db.execute("SELECT * FROM players WHERE token=?", (token,)).fetchone()
            if player:
                db.execute("UPDATE players SET telegram_chat_id=? WHERE token=?", (chat_id,token))
                await send_telegram(chat_id, f"✅ Привет, <b>{player['name']}</b>! Ты подключён к турниру прогнозов ⚽\nУдачи! 🏆")
            else:
                await send_telegram(chat_id, "❌ Неверный токен.")
    return {"ok":True}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
