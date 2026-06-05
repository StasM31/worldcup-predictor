from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
import os, httpx, asyncio, secrets
from contextlib import contextmanager
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme123")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BACKUP_ADMIN_CHAT_ID = os.environ.get("BACKUP_CHAT_ID", "")
GRACE_SECONDS = 60
MATCH_DURATION_SECONDS = 120 * 60

@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            telegram_chat_id TEXT,
            token TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS matches (
            id SERIAL PRIMARY KEY,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            match_time TEXT NOT NULL,
            started_at TEXT,
            home_score INTEGER,
            away_score INTEGER,
            status TEXT DEFAULT 'upcoming'
        );
        CREATE TABLE IF NOT EXISTS predictions (
            id SERIAL PRIMARY KEY,
            player_id INTEGER NOT NULL REFERENCES players(id),
            match_id INTEGER NOT NULL REFERENCES matches(id),
            home_score INTEGER NOT NULL,
            away_score INTEGER NOT NULL,
            points INTEGER DEFAULT 0,
            is_vabank INTEGER DEFAULT 0,
            created_at TEXT DEFAULT to_char(now(),'YYYY-MM-DD HH24:MI:SS'),
            UNIQUE(player_id, match_id)
        );
        CREATE TABLE IF NOT EXISTS vabank_used (
            player_id INTEGER PRIMARY KEY REFERENCES players(id)
        );
        CREATE TABLE IF NOT EXISTS tournament_predictions (
            id SERIAL PRIMARY KEY,
            player_id INTEGER UNIQUE NOT NULL REFERENCES players(id),
            champion TEXT,
            finalist1 TEXT,
            finalist2 TEXT,
            top_scorer TEXT,
            champion_pts INTEGER DEFAULT 0,
            finalist_pts INTEGER DEFAULT 0,
            scorer_pts INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tournament_result (
            id INTEGER PRIMARY KEY CHECK (id=1),
            champion TEXT,
            finalist1 TEXT,
            finalist2 TEXT,
            top_scorer TEXT
        );
        """)

init_db()

# ── Авто-старт ──
async def auto_start_scheduler():
    from datetime import timedelta
    await asyncio.sleep(5)
    while True:
        try:
            now_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None)
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM matches WHERE status='upcoming'")
                matches = cur.fetchall()
                for m in matches:
                    mt = parse_dt(m["match_time"])
                    if mt and now_msk >= mt.replace(tzinfo=None):
                        cur.execute("UPDATE matches SET status='grace', started_at=%s WHERE id=%s",
                                   (datetime.now(timezone.utc).isoformat(), m["id"]))
                        asyncio.create_task(grace_period_end(m["id"]))
                        print(f"Auto-started: {m['home_team']} vs {m['away_team']}")
        except Exception as e:
            print(f"Scheduler error: {e}")
        await asyncio.sleep(30)

# ── Ежедневный дайджест в 18:00 МСК ──
DAILY_DIGEST_HOUR_MSK = 18

async def daily_digest_scheduler():
    from datetime import timedelta
    await asyncio.sleep(10)
    last_sent_date = None
    while True:
        try:
            now_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None)
            today = now_msk.date()
            if now_msk.hour == DAILY_DIGEST_HOUR_MSK and now_msk.minute < 1 and last_sent_date != today:
                last_sent_date = today
                await send_daily_digest()
        except Exception as e:
            print(f"Daily digest error: {e}")
        await asyncio.sleep(30)

async def send_daily_digest():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT pl.name,
                   COALESCE(SUM(p.points),0) +
                   COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as total_points,
                   COUNT(p.id) as pred_count,
                   SUM(CASE WHEN p.points>=3 THEN 1 ELSE 0 END) as exact_hits,
                   SUM(CASE WHEN p.points=1 THEN 1 ELSE 0 END) as outcome_hits
            FROM players pl
            LEFT JOIN predictions p ON pl.id=p.player_id
            GROUP BY pl.id ORDER BY total_points DESC
        """)
        rows = cur.fetchall()
        cur.execute("SELECT telegram_chat_id FROM players WHERE telegram_chat_id IS NOT NULL")
        players = cur.fetchall()
    if not rows:
        return
    from datetime import timedelta
    medals = ['🥇','🥈','🥉']
    today_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime('%d.%m.%Y')
    lines = [f"📊 <b>Таблица лидеров на {today_msk}</b>\n"]
    for i, r in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} <b>{r['name']}</b> — <b>{r['total_points']} очк.</b>  🎯{r['exact_hits'] or 0} ✅{r['outcome_hits'] or 0}")
    text = "\n".join(lines)
    tasks = [send_telegram(str(pl["telegram_chat_id"]), text) for pl in players]
    if tasks:
        await asyncio.gather(*tasks)

# ── Ежедневный бэкап в 03:00 МСК ──
BACKUP_HOUR_MSK = 3

async def daily_backup_scheduler():
    from datetime import timedelta
    await asyncio.sleep(15)
    last_backup_date = None
    while True:
        try:
            now_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None)
            today = now_msk.date()
            if now_msk.hour == BACKUP_HOUR_MSK and now_msk.minute < 1 and last_backup_date != today:
                last_backup_date = today
                await send_backup()
        except Exception as e:
            print(f"Backup error: {e}")
        await asyncio.sleep(30)

async def send_backup():
    """Дамп всех данных в JSON и отправка в Telegram."""
    chat_id = BACKUP_ADMIN_CHAT_ID
    if not BOT_TOKEN or not chat_id:
        print("Backup: BOT_TOKEN or BACKUP_CHAT_ID not set")
        return
    try:
        import json
        from datetime import timedelta
        with get_db() as conn:
            cur = conn.cursor()
            data = {}
            for table in ["players","matches","predictions","vabank_used","tournament_predictions","tournament_result"]:
                cur.execute(f"SELECT * FROM {table}")
                rows = cur.fetchall()
                data[table] = [dict(r) for r in rows]
        now_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%d.%m.%Y_%H-%M")
        backup_json = json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": chat_id, "caption": f"🗄 Бэкап базы данных\n📅 {now_msk} МСК"},
                files={"document": (f"backup_{now_msk}.json", backup_json, "application/json")},
                timeout=30
            )
        print(f"Backup sent to {chat_id}")
    except Exception as e:
        print(f"Backup send error: {e}")

@app.on_event("startup")
async def startup():
    asyncio.create_task(auto_start_scheduler())
    asyncio.create_task(daily_digest_scheduler())
    asyncio.create_task(daily_backup_scheduler())

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

@app.post("/api/admin/send-digest", dependencies=[Depends(require_admin)])
async def manual_digest():
    await send_daily_digest()
    return {"ok": True}

@app.post("/api/admin/send-backup", dependencies=[Depends(require_admin)])
async def manual_backup():
    await send_backup()
    return {"ok": True}

def get_player_by_token(token: str):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM players WHERE token=%s", (token,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(401, "Неверный токен")
    return row

# ── Флаги ──
TEAM_FLAGS = {
    'россия':'🇷🇺','германия':'🇩🇪','франция':'🇫🇷','испания':'🇪🇸','италия':'🇮🇹',
    'бразилия':'🇧🇷','аргентина':'🇦🇷','португалия':'🇵🇹','нидерланды':'🇳🇱','голландия':'🇳🇱',
    'англия':'🏴󠁧󠁢󠁥󠁮󠁧󠁿','великобритания':'🇬🇧','бельгия':'🇧🇪','хорватия':'🇭🇷','дания':'🇩🇰',
    'швейцария':'🇨🇭','польша':'🇵🇱','швеция':'🇸🇪','норвегия':'🇳🇴','австрия':'🇦🇹',
    'чехия':'🇨🇿','венгрия':'🇭🇺','румыния':'🇷🇴','сербия':'🇷🇸','греция':'🇬🇷',
    'турция':'🇹🇷','украина':'🇺🇦','сша':'🇺🇸','мексика':'🇲🇽','канада':'🇨🇦',
    'япония':'🇯🇵','южная корея':'🇰🇷','корея':'🇰🇷','австралия':'🇦🇺','иран':'🇮🇷',
    'саудовская аравия':'🇸🇦','марокко':'🇲🇦','сенегал':'🇸🇳','гана':'🇬🇭','камерун':'🇨🇲',
    'нигерия':'🇳🇬','египет':'🇪🇬','тунис':'🇹🇳','эквадор':'🇪🇨','уругвай':'🇺🇾',
    'колумбия':'🇨🇴','чили':'🇨🇱','перу':'🇵🇪','катар':'🇶🇦','ирак':'🇮🇶',
    'израиль':'🇮🇱','словакия':'🇸🇰','словения':'🇸🇮','болгария':'🇧🇬','финляндия':'🇫🇮',
    'шотландия':'🏴󠁧󠁢󠁳󠁣󠁴󠁿','уэльс':'🏴󠁧󠁢󠁷󠁬󠁳󠁿','ирландия':'🇮🇪','алжир':'🇩🇿',
    'иордания':'🇯🇴','узбекистан':'🇺🇿','новая зеландия':'🇳🇿','гаити':'🇭🇹',
    'кюрасао':'🇨🇼',"кот-д'ивуар":'🇨🇮','кабо-верде':'🇨🇻','юар':'🇿🇦',
    'босния и герцеговина':'🇧🇦','др конго':'🇨🇩','парагвай':'🇵🇾','армения':'🇦🇲',
    'панама':'🇵🇦','гондурас':'🇭🇳','коста-рика':'🇨🇷','венесуэла':'🇻🇪','боливия':'🇧🇴',
    'russia':'🇷🇺','germany':'🇩🇪','france':'🇫🇷','spain':'🇪🇸','italy':'🇮🇹',
    'brazil':'🇧🇷','argentina':'🇦🇷','portugal':'🇵🇹','netherlands':'🇳🇱',
    'england':'🏴󠁧󠁢󠁥󠁮󠁧󠁿','belgium':'🇧🇪','croatia':'🇭🇷','denmark':'🇩🇰',
    'switzerland':'🇨🇭','poland':'🇵🇱','sweden':'🇸🇪','norway':'🇳🇴','austria':'🇦🇹',
    'czech republic':'🇨🇿','czechia':'🇨🇿','hungary':'🇭🇺','romania':'🇷🇴',
    'serbia':'🇷🇸','greece':'🇬🇷','turkey':'🇹🇷','ukraine':'🇺🇦',
    'usa':'🇺🇸','united states':'🇺🇸','mexico':'🇲🇽','canada':'🇨🇦',
    'japan':'🇯🇵','south korea':'🇰🇷','australia':'🇦🇺','iran':'🇮🇷',
    'saudi arabia':'🇸🇦','morocco':'🇲🇦','senegal':'🇸🇳','ghana':'🇬🇭',
    'cameroon':'🇨🇲','nigeria':'🇳🇬','egypt':'🇪🇬','tunisia':'🇹🇳',
    'ecuador':'🇪🇨','uruguay':'🇺🇾','colombia':'🇨🇴','chile':'🇨🇱',
    'peru':'🇵🇪','qatar':'🇶🇦','algeria':'🇩🇿','jordan':'🇯🇴',
    'uzbekistan':'🇺🇿','new zealand':'🇳🇿','haiti':'🇭🇹','curacao':'🇨🇼',
    'south africa':'🇿🇦','bosnia':'🇧🇦','drc':'🇨🇩','panama':'🇵🇦',
    'paraguay':'🇵🇾','armenia':'🇦🇲','scotland':'🏴󠁧󠁢󠁳󠁣󠁴󠁿',
}

def get_flag(name):
    if not name: return ''
    key = name.strip().lower()
    flag = TEAM_FLAGS.get(key,'')
    if not flag:
        for k,v in TEAM_FLAGS.items():
            if key in k or k in key:
                flag = v; break
    return flag

def team_with_flag(name):
    flag = get_flag(name)
    return f"{name} {flag}" if flag else name

async def send_telegram(chat_id, text):
    if not BOT_TOKEN or not chat_id: return
    async with httpx.AsyncClient() as client:
        try:
            await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                              json={"chat_id":chat_id,"text":text,"parse_mode":"HTML"})
        except Exception as e:
            print(f"Telegram error: {e}")

async def broadcast_predictions(match_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM matches WHERE id=%s", (match_id,))
        match = cur.fetchone()
        cur.execute("""SELECT pl.name,p.home_score,p.away_score,p.is_vabank
            FROM predictions p JOIN players pl ON p.player_id=pl.id WHERE p.match_id=%s""", (match_id,))
        preds = cur.fetchall()
        cur.execute("SELECT id,name,telegram_chat_id FROM players")
        all_players = cur.fetchall()
    if not match: return
    home = team_with_flag(match['home_team'])
    away = team_with_flag(match['away_team'])
    pred_map = {p["name"]: p for p in preds}
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
    if tasks:
        await asyncio.gather(*tasks)

def check_and_broadcast(match_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM players")
        total = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM predictions WHERE match_id=%s", (match_id,))
        done = cur.fetchone()["c"]
    if total > 0 and done >= total:
        asyncio.create_task(broadcast_predictions(match_id))

async def grace_period_end(match_id):
    await asyncio.sleep(GRACE_SECONDS)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status FROM matches WHERE id=%s", (match_id,))
        m = cur.fetchone()
        if m and m["status"] == "grace":
            cur.execute("UPDATE matches SET status='live' WHERE id=%s", (match_id,))
    await broadcast_predictions(match_id)
    asyncio.create_task(auto_finish_match(match_id))

async def auto_finish_match(match_id):
    await asyncio.sleep(MATCH_DURATION_SECONDS - GRACE_SECONDS)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status FROM matches WHERE id=%s", (match_id,))
        m = cur.fetchone()
        if m and m["status"] == "live":
            cur.execute("UPDATE matches SET status='ended' WHERE id=%s", (match_id,))
            print(f"Auto-ended match {match_id}")

def calc_points(ph, pa, rh, ra, is_vabank=False):
    if ph == rh and pa == ra:
        return 9 if is_vabank else 3
    po = "H" if ph>pa else ("A" if ph<pa else "D")
    ro = "H" if rh>ra else ("A" if rh<ra else "D")
    if po == ro:
        return 3 if is_vabank else 1
    return 0

# ── Player endpoints ──
class PredictionIn(BaseModel):
    token: str
    home_score: int
    away_score: int
    is_vabank: bool = False

@app.get("/api/me")
def get_me(token: str):
    player = get_player_by_token(token)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM vabank_used WHERE player_id=%s", (player["id"],))
        vb = cur.fetchone()
    return {"id": player["id"], "name": player["name"], "vabank_used": bool(vb)}

@app.get("/api/matches")
def list_matches(token: str):
    player = get_player_by_token(token)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM matches ORDER BY match_time ASC")
        matches = cur.fetchall()
        cur.execute("SELECT match_id,home_score,away_score,is_vabank FROM predictions WHERE player_id=%s", (player["id"],))
        my_preds = cur.fetchall()
    pred_map = {p["match_id"]: p for p in my_preds}
    result = []
    for m in matches:
        d = dict(m)
        d["my_prediction"] = dict(pred_map[m["id"]]) if m["id"] in pred_map else None
        result.append(d)
    return result

@app.post("/api/predict/{match_id}")
async def predict(match_id: int, body: PredictionIn):
    player = get_player_by_token(body.token)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM matches WHERE id=%s", (match_id,))
        match = cur.fetchone()
        if not match:
            raise HTTPException(404, "Матч не найден")
        if match["status"] not in ("upcoming","grace"):
            raise HTTPException(400, "Приём ставок закрыт")
        cur.execute("SELECT id,is_vabank FROM predictions WHERE player_id=%s AND match_id=%s", (player["id"],match_id))
        existing = cur.fetchone()
        if existing and match["status"] == "grace":
            raise HTTPException(400, "Матч уже начался, прогноз менять нельзя")
        is_vabank = 0
        if body.is_vabank:
            if not existing:
                cur.execute("SELECT 1 FROM vabank_used WHERE player_id=%s", (player["id"],))
                if cur.fetchone():
                    raise HTTPException(400, "Ва-банк уже использован в этом турнире")
                cur.execute("INSERT INTO vabank_used (player_id) VALUES (%s) ON CONFLICT DO NOTHING", (player["id"],))
            is_vabank = 1
        if existing:
            if existing["is_vabank"] and not is_vabank:
                cur.execute("DELETE FROM vabank_used WHERE player_id=%s", (player["id"],))
            cur.execute("UPDATE predictions SET home_score=%s,away_score=%s,is_vabank=%s WHERE player_id=%s AND match_id=%s",
                       (body.home_score,body.away_score,is_vabank,player["id"],match_id))
        else:
            cur.execute("INSERT INTO predictions (player_id,match_id,home_score,away_score,is_vabank) VALUES (%s,%s,%s,%s,%s)",
                       (player["id"],match_id,body.home_score,body.away_score,is_vabank))
    check_and_broadcast(match_id)
    return {"ok": True}

@app.get("/api/match/{match_id}/predictions")
def match_predictions(match_id: int, token: str):
    get_player_by_token(token)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM matches WHERE id=%s", (match_id,))
        match = cur.fetchone()
        if not match: raise HTTPException(404)
        cur.execute("SELECT COUNT(*) as c FROM players")
        total = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM predictions WHERE match_id=%s", (match_id,))
        done = cur.fetchone()["c"]
        if match["status"] == "upcoming":
            return {"hidden":True,"reason":f"Прогнозы скрыты до начала матча • {done}/{total} сделали ставку"}
        cur.execute("""SELECT pl.name,p.home_score,p.away_score,p.points,p.is_vabank
            FROM predictions p JOIN players pl ON p.player_id=pl.id
            WHERE p.match_id=%s ORDER BY pl.name""", (match_id,))
        preds = cur.fetchall()
    return {"hidden":False,"predictions":[dict(p) for p in preds],"match":dict(match)}

@app.get("/api/leaderboard")
def leaderboard(token: str):
    get_player_by_token(token)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT pl.name,
                   COALESCE(SUM(p.points),0) +
                   COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as total_points,
                   COALESCE(SUM(p.points),0) as match_points,
                   COUNT(p.id) as predictions_count,
                   SUM(CASE WHEN p.points>=3 THEN 1 ELSE 0 END) as exact_hits,
                   SUM(CASE WHEN p.points=1 THEN 1 ELSE 0 END) as outcome_hits,
                   CASE WHEN COUNT(p.id)>0 THEN ROUND(100.0*SUM(CASE WHEN p.points>0 THEN 1 ELSE 0 END)/COUNT(p.id),1) ELSE 0 END as hit_pct,
                   COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as tournament_bonus
            FROM players pl LEFT JOIN predictions p ON pl.id=p.player_id
            GROUP BY pl.id ORDER BY total_points DESC
        """)
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.get("/api/archive")
def archive(token: str):
    get_player_by_token(token)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM matches WHERE status='finished' ORDER BY match_time DESC")
        matches = cur.fetchall()
        result = []
        for m in matches:
            cur.execute("""SELECT pl.name,p.home_score,p.away_score,p.points,p.is_vabank
                FROM predictions p JOIN players pl ON p.player_id=pl.id
                WHERE p.match_id=%s ORDER BY p.points DESC,pl.name""", (m["id"],))
            preds = cur.fetchall()
            d = dict(m)
            d["predictions"] = [dict(p) for p in preds]
            result.append(d)
    return result

class TournamentPredIn(BaseModel):
    token: str
    champion: str
    finalist1: str
    finalist2: str
    top_scorer: str

@app.post("/api/tournament-prediction")
def set_tournament_prediction(body: TournamentPredIn):
    player = get_player_by_token(body.token)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO tournament_predictions (player_id,champion,finalist1,finalist2,top_scorer)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT(player_id) DO UPDATE SET
              champion=EXCLUDED.champion,finalist1=EXCLUDED.finalist1,
              finalist2=EXCLUDED.finalist2,top_scorer=EXCLUDED.top_scorer""",
            (player["id"],body.champion,body.finalist1,body.finalist2,body.top_scorer))
    return {"ok": True}

@app.get("/api/tournament-prediction")
def get_tournament_prediction(token: str):
    player = get_player_by_token(token)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tournament_predictions WHERE player_id=%s", (player["id"],))
        row = cur.fetchone()
        cur.execute("SELECT * FROM tournament_result WHERE id=1")
        result = cur.fetchone()
    return {"my_prediction":dict(row) if row else None,"result":dict(result) if result else None}

@app.get("/api/tournament-predictions-all")
def get_all_tournament_predictions(token: str):
    player = get_player_by_token(token)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM matches WHERE status != 'upcoming'")
        tournament_started = cur.fetchone()["c"] > 0
        cur.execute("SELECT COUNT(*) as c FROM players")
        total_players = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM tournament_predictions")
        done_players = cur.fetchone()["c"]
        cur.execute("""SELECT pl.name,tp.champion,tp.finalist1,tp.finalist2,tp.top_scorer,
                   tp.champion_pts,tp.finalist_pts,tp.scorer_pts,pl.id as player_id
            FROM tournament_predictions tp JOIN players pl ON tp.player_id=pl.id""")
        rows = cur.fetchall()
        cur.execute("SELECT * FROM tournament_result WHERE id=1")
        result = cur.fetchone()
    if tournament_started:
        return {"predictions":[dict(r) for r in rows],"result":dict(result) if result else None,
                "tournament_started":True,"done_count":done_players,"total_count":total_players}
    else:
        my_pred = next((dict(r) for r in rows if r["player_id"]==player["id"]),None)
        return {"predictions":[my_pred] if my_pred else [],"result":None,
                "tournament_started":False,"done_count":done_players,"total_count":total_players}

# ── Admin ──
class PlayerIn(BaseModel):
    name: str
    telegram_chat_id: Optional[str] = None

class MatchIn(BaseModel):
    home_team: str
    away_team: str
    match_time: str

class MatchBatchIn(BaseModel):
    matches: List[MatchIn]

class ResultIn(BaseModel):
    home_score: int
    away_score: int

class TournamentResultIn(BaseModel):
    champion: str
    finalist1: str
    finalist2: str
    top_scorer: str

@app.post("/api/admin/players", dependencies=[Depends(require_admin)])
def add_player(body: PlayerIn):
    token = secrets.token_urlsafe(16)
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO players (name,telegram_chat_id,token) VALUES (%s,%s,%s)",
                       (body.name,body.telegram_chat_id,token))
        except Exception:
            raise HTTPException(400, "Участник уже существует")
    return {"name":body.name,"token":token}

@app.get("/api/admin/players", dependencies=[Depends(require_admin)])
def get_players():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id,name,telegram_chat_id,token FROM players")
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.post("/api/admin/matches", dependencies=[Depends(require_admin)])
def add_match(body: MatchIn):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO matches (home_team,away_team,match_time) VALUES (%s,%s,%s) RETURNING id",
                   (body.home_team,body.away_team,body.match_time))
        row = cur.fetchone()
    return {"id": row["id"]}

@app.post("/api/admin/matches/batch", dependencies=[Depends(require_admin)])
def add_matches_batch(body: MatchBatchIn):
    added = 0
    with get_db() as conn:
        cur = conn.cursor()
        for m in body.matches:
            try:
                cur.execute("INSERT INTO matches (home_team,away_team,match_time) VALUES (%s,%s,%s)",
                           (m.home_team,m.away_team,m.match_time))
                added += 1
            except Exception as e:
                print(f"Batch error: {e}")
    return {"added": added}

@app.delete("/api/admin/matches/{match_id}", dependencies=[Depends(require_admin)])
def delete_match(match_id: int):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM predictions WHERE match_id=%s", (match_id,))
        cur.execute("DELETE FROM matches WHERE id=%s", (match_id,))
    return {"ok": True}

@app.post("/api/admin/matches/{match_id}/result", dependencies=[Depends(require_admin)])
def set_result(match_id: int, body: ResultIn):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE matches SET home_score=%s,away_score=%s,status='finished' WHERE id=%s",
                   (body.home_score,body.away_score,match_id))
        cur.execute("SELECT id,home_score,away_score,is_vabank FROM predictions WHERE match_id=%s", (match_id,))
        preds = cur.fetchall()
        for p in preds:
            pts = calc_points(p["home_score"],p["away_score"],body.home_score,body.away_score,bool(p["is_vabank"]))
            cur.execute("UPDATE predictions SET points=%s WHERE id=%s", (pts,p["id"]))
    return {"ok": True}

@app.get("/api/admin/matches", dependencies=[Depends(require_admin)])
def admin_matches():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM matches ORDER BY match_time ASC")
        matches = cur.fetchall()
        cur.execute("""SELECT p.match_id,pl.name,p.home_score,p.away_score,p.points,p.is_vabank
            FROM predictions p JOIN players pl ON p.player_id=pl.id""")
        preds = cur.fetchall()
    pred_map = {}
    for p in preds:
        pred_map.setdefault(p["match_id"],[]).append(dict(p))
    return [dict(m)|{"predictions":pred_map.get(m["id"],[])} for m in matches]

@app.post("/api/admin/tournament-result", dependencies=[Depends(require_admin)])
def set_tournament_result(body: TournamentResultIn):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO tournament_result (id,champion,finalist1,finalist2,top_scorer)
            VALUES (1,%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET
              champion=EXCLUDED.champion,finalist1=EXCLUDED.finalist1,
              finalist2=EXCLUDED.finalist2,top_scorer=EXCLUDED.top_scorer""",
            (body.champion,body.finalist1,body.finalist2,body.top_scorer))
        cur.execute("SELECT * FROM tournament_predictions")
        preds = cur.fetchall()
        for tp in preds:
            champ_pts = 10 if tp["champion"] and tp["champion"].lower().strip()==body.champion.lower().strip() else 0
            finalists = {body.finalist1.lower().strip(),body.finalist2.lower().strip()}
            fin_pts = 0
            if tp["finalist1"] and tp["finalist1"].lower().strip() in finalists: fin_pts += 5
            if tp["finalist2"] and tp["finalist2"].lower().strip() in finalists: fin_pts += 5
            scorer_pts = 10 if tp["top_scorer"] and tp["top_scorer"].lower().strip()==body.top_scorer.lower().strip() else 0
            cur.execute("UPDATE tournament_predictions SET champion_pts=%s,finalist_pts=%s,scorer_pts=%s WHERE player_id=%s",
                       (champ_pts,fin_pts,scorer_pts,tp["player_id"]))
    return {"ok": True}

@app.post("/api/telegram/webhook")
async def telegram_webhook(update: dict):
    msg = update.get("message",{})
    text = msg.get("text","")
    chat_id = str(msg.get("chat",{}).get("id",""))
    if text.startswith("/start "):
        token = text.split(" ",1)[1].strip()
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM players WHERE token=%s", (token,))
            player = cur.fetchone()
            if player:
                cur.execute("UPDATE players SET telegram_chat_id=%s WHERE token=%s", (chat_id,token))
                await send_telegram(chat_id, f"✅ Привет, <b>{player['name']}</b>! Ты подключён к турниру прогнозов ⚽\nТеперь будешь получать уведомления перед матчами. Удачи! 🏆")
            else:
                await send_telegram(chat_id, "❌ Неверный токен.")
    return {"ok": True}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
