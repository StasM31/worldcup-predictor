from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
import os, httpx, asyncio, secrets
from datetime import datetime, timezone
import asyncpg

app = FastAPI()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme123")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BACKUP_ADMIN_CHAT_ID = os.environ.get("BACKUP_CHAT_ID", "")
GRACE_SECONDS = 60
MATCH_DURATION_SECONDS = 120 * 60

_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
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
            champion TEXT, finalist1 TEXT, finalist2 TEXT, top_scorer TEXT,
            champion_pts INTEGER DEFAULT 0, finalist_pts INTEGER DEFAULT 0, scorer_pts INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tournament_result (
            id INTEGER PRIMARY KEY CHECK (id=1),
            champion TEXT, finalist1 TEXT, finalist2 TEXT, top_scorer TEXT
        );
        """)

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

async def get_player_by_token(token: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM players WHERE token=$1", token)
    if not row:
        raise HTTPException(401, "Неверный токен")
    return dict(row)

# ── Telegram ──
TEAM_FLAGS = {
    'россия':'🇷🇺','германия':'🇩🇪','франция':'🇫🇷','испания':'🇪🇸','италия':'🇮🇹',
    'бразилия':'🇧🇷','аргентина':'🇦🇷','португалия':'🇵🇹','нидерланды':'🇳🇱',
    'англия':'🏴󠁧󠁢󠁥󠁮󠁧󠁿','великобритания':'🇬🇧','бельгия':'🇧🇪','хорватия':'🇭🇷',
    'дания':'🇩🇰','швейцария':'🇨🇭','польша':'🇵🇱','швеция':'🇸🇪','норвегия':'🇳🇴',
    'австрия':'🇦🇹','чехия':'🇨🇿','венгрия':'🇭🇺','румыния':'🇷🇴','сербия':'🇷🇸',
    'греция':'🇬🇷','турция':'🇹🇷','украина':'🇺🇦','сша':'🇺🇸','мексика':'🇲🇽',
    'канада':'🇨🇦','япония':'🇯🇵','южная корея':'🇰🇷','корея':'🇰🇷','австралия':'🇦🇺',
    'иран':'🇮🇷','саудовская аравия':'🇸🇦','марокко':'🇲🇦','сенегал':'🇸🇳','гана':'🇬🇭',
    'камерун':'🇨🇲','нигерия':'🇳🇬','египет':'🇪🇬','тунис':'🇹🇳','эквадор':'🇪🇨',
    'уругвай':'🇺🇾','колумбия':'🇨🇴','чили':'🇨🇱','перу':'🇵🇪','катар':'🇶🇦',
    'ирак':'🇮🇶','израиль':'🇮🇱','словакия':'🇸🇰','финляндия':'🇫🇮','шотландия':'🏴󠁧󠁢󠁳󠁣󠁴󠁿',
    'ирландия':'🇮🇪','алжир':'🇩🇿','иордания':'🇯🇴','узбекистан':'🇺🇿','гаити':'🇭🇹',
    'кюрасао':'🇨🇼',"кот-д'ивуар":'🇨🇮','кабо-верде':'🇨🇻','юар':'🇿🇦',
    'босния и герцеговина':'🇧🇦','др конго':'🇨🇩','парагвай':'🇵🇾','армения':'🇦🇲',
    'панама':'🇵🇦','венесуэла':'🇻🇪','новая зеландия':'🇳🇿',
    'russia':'🇷🇺','germany':'🇩🇪','france':'🇫🇷','spain':'🇪🇸','italy':'🇮🇹',
    'brazil':'🇧🇷','argentina':'🇦🇷','portugal':'🇵🇹','netherlands':'🇳🇱',
    'england':'🏴󠁧󠁢󠁥󠁮󠁧󠁿','belgium':'🇧🇪','croatia':'🇭🇷','denmark':'🇩🇰',
    'switzerland':'🇨🇭','poland':'🇵🇱','sweden':'🇸🇪','norway':'🇳🇴','austria':'🇦🇹',
    'czech republic':'🇨🇿','hungary':'🇭🇺','romania':'🇷🇴','serbia':'🇷🇸',
    'greece':'🇬🇷','turkey':'🇹🇷','ukraine':'🇺🇦','usa':'🇺🇸','mexico':'🇲🇽',
    'canada':'🇨🇦','japan':'🇯🇵','south korea':'🇰🇷','australia':'🇦🇺','iran':'🇮🇷',
    'saudi arabia':'🇸🇦','morocco':'🇲🇦','senegal':'🇸🇳','ghana':'🇬🇭','egypt':'🇪🇬',
    'ecuador':'🇪🇨','uruguay':'🇺🇾','colombia':'🇨🇴','qatar':'🇶🇦','algeria':'🇩🇿',
    'jordan':'🇯🇴','uzbekistan':'🇺🇿','haiti':'🇭🇹','curacao':'🇨🇼','south africa':'🇿🇦',
    'bosnia':'🇧🇦','paraguay':'🇵🇾','armenia':'🇦🇲','scotland':'🏴󠁧󠁢󠁳󠁣󠁴󠁿','panama':'🇵🇦',
}
def get_flag(name):
    if not name: return ''
    key = name.strip().lower()
    flag = TEAM_FLAGS.get(key,'')
    if not flag:
        for k,v in TEAM_FLAGS.items():
            if key in k or k in key: flag=v; break
    return flag
def team_with_flag(name):
    f = get_flag(name)
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        match = await conn.fetchrow("SELECT * FROM matches WHERE id=$1", match_id)
        preds = await conn.fetch("""SELECT pl.name,p.home_score,p.away_score,p.is_vabank
            FROM predictions p JOIN players pl ON p.player_id=pl.id WHERE p.match_id=$1""", match_id)
        all_players = await conn.fetch("SELECT id,name,telegram_chat_id FROM players")
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

async def check_and_broadcast(match_id):
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM players")
        done = await conn.fetchval("SELECT COUNT(*) FROM predictions WHERE match_id=$1", match_id)
    if total > 0 and done >= total:
        asyncio.create_task(broadcast_predictions(match_id))

async def grace_period_end(match_id):
    await asyncio.sleep(GRACE_SECONDS)
    pool = await get_pool()
    async with pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM matches WHERE id=$1", match_id)
        if status == "grace":
            await conn.execute("UPDATE matches SET status='live' WHERE id=$1", match_id)
    await broadcast_predictions(match_id)
    asyncio.create_task(auto_finish_match(match_id))

async def auto_finish_match(match_id):
    await asyncio.sleep(MATCH_DURATION_SECONDS - GRACE_SECONDS)
    pool = await get_pool()
    async with pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM matches WHERE id=$1", match_id)
        if status == "live":
            await conn.execute("UPDATE matches SET status='ended' WHERE id=$1", match_id)

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
            pool = await get_pool()
            async with pool.acquire() as conn:
                matches = await conn.fetch("SELECT * FROM matches WHERE status='upcoming'")
                for m in matches:
                    mt = parse_dt(m["match_time"])
                    if mt and now_msk >= mt.replace(tzinfo=None):
                        await conn.execute("UPDATE matches SET status='grace', started_at=$1 WHERE id=$2",
                                          datetime.now(timezone.utc).isoformat(), m["id"])
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT pl.name,
                   COALESCE(SUM(p.points),0)+COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as total_points,
                   COUNT(p.id) as pred_count,
                   SUM(CASE WHEN p.points>=3 THEN 1 ELSE 0 END) as exact_hits,
                   SUM(CASE WHEN p.points=1 THEN 1 ELSE 0 END) as outcome_hits
            FROM players pl LEFT JOIN predictions p ON pl.id=p.player_id
            GROUP BY pl.id ORDER BY total_points DESC""")
        players = await conn.fetch("SELECT telegram_chat_id FROM players WHERE telegram_chat_id IS NOT NULL")
    if not rows: return
    medals=['🥇','🥈','🥉']
    today_msk=(datetime.now(timezone.utc)+timedelta(hours=3)).strftime('%d.%m.%Y')
    lines=[f"📊 <b>Таблица лидеров на {today_msk}</b>\n"]
    for i,r in enumerate(rows):
        medal=medals[i] if i<3 else f"{i+1}."
        lines.append(f"{medal} <b>{r['name']}</b> — <b>{r['total_points']} очк.</b>  🎯{r['exact_hits'] or 0} ✅{r['outcome_hits'] or 0}")
    text="\n".join(lines)
    tasks=[send_telegram(str(pl["telegram_chat_id"]),text) for pl in players]
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
    try:
        import json
        from datetime import timedelta
        pool = await get_pool()
        data = {}
        async with pool.acquire() as conn:
            for table in ["players","matches","predictions","vabank_used","tournament_predictions","tournament_result"]:
                rows = await conn.fetch(f"SELECT * FROM {table}")
                data[table] = [dict(r) for r in rows]
        now_msk=(datetime.now(timezone.utc)+timedelta(hours=3)).strftime("%d.%m.%Y_%H-%M")
        backup_json=json.dumps(data,ensure_ascii=False,indent=2,default=str).encode("utf-8")
        async with httpx.AsyncClient() as client:
            await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id":chat_id,"caption":f"🗄 Бэкап базы данных\n📅 {now_msk} МСК"},
                files={"document":(f"backup_{now_msk}.json",backup_json,"application/json")},timeout=30)
    except Exception as e:
        print(f"Backup send error: {e}")

@app.on_event("startup")
async def startup():
    await init_db()
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
async def get_me(token: str):
    player = await get_player_by_token(token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        vb = await conn.fetchrow("SELECT 1 FROM vabank_used WHERE player_id=$1", player["id"])
    return {"id":player["id"],"name":player["name"],"vabank_used":bool(vb)}

@app.get("/api/matches")
async def list_matches(token: str):
    player = await get_player_by_token(token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        matches = await conn.fetch("SELECT * FROM matches ORDER BY match_time ASC")
        my_preds = await conn.fetch("SELECT match_id,home_score,away_score,is_vabank FROM predictions WHERE player_id=$1", player["id"])
    pred_map = {p["match_id"]: dict(p) for p in my_preds}
    result = []
    for m in matches:
        d = dict(m)
        d["my_prediction"] = pred_map.get(m["id"])
        result.append(d)
    return result

@app.post("/api/predict/{match_id}")
async def predict(match_id: int, body: PredictionIn):
    player = await get_player_by_token(body.token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        match = await conn.fetchrow("SELECT * FROM matches WHERE id=$1", match_id)
        if not match: raise HTTPException(404,"Матч не найден")
        if match["status"] not in ("upcoming","grace"): raise HTTPException(400,"Приём ставок закрыт")
        existing = await conn.fetchrow("SELECT id,is_vabank FROM predictions WHERE player_id=$1 AND match_id=$2", player["id"],match_id)
        if existing and match["status"]=="grace": raise HTTPException(400,"Матч уже начался")
        is_vabank = 0
        if body.is_vabank:
            if not existing:
                vb = await conn.fetchrow("SELECT 1 FROM vabank_used WHERE player_id=$1", player["id"])
                if vb: raise HTTPException(400,"Ва-банк уже использован")
                await conn.execute("INSERT INTO vabank_used (player_id) VALUES ($1) ON CONFLICT DO NOTHING", player["id"])
            is_vabank = 1
        if existing:
            if existing["is_vabank"] and not is_vabank:
                await conn.execute("DELETE FROM vabank_used WHERE player_id=$1", player["id"])
            await conn.execute("UPDATE predictions SET home_score=$1,away_score=$2,is_vabank=$3 WHERE player_id=$4 AND match_id=$5",
                              body.home_score,body.away_score,is_vabank,player["id"],match_id)
        else:
            await conn.execute("INSERT INTO predictions (player_id,match_id,home_score,away_score,is_vabank) VALUES ($1,$2,$3,$4,$5)",
                              player["id"],match_id,body.home_score,body.away_score,is_vabank)
    await check_and_broadcast(match_id)
    return {"ok":True}

@app.get("/api/match/{match_id}/predictions")
async def match_predictions(match_id: int, token: str):
    await get_player_by_token(token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        match = await conn.fetchrow("SELECT * FROM matches WHERE id=$1", match_id)
        if not match: raise HTTPException(404)
        total = await conn.fetchval("SELECT COUNT(*) FROM players")
        done = await conn.fetchval("SELECT COUNT(*) FROM predictions WHERE match_id=$1", match_id)
        if match["status"]=="upcoming":
            return {"hidden":True,"reason":f"Прогнозы скрыты до начала матча • {done}/{total} сделали ставку"}
        preds = await conn.fetch("""SELECT pl.name,p.home_score,p.away_score,p.points,p.is_vabank
            FROM predictions p JOIN players pl ON p.player_id=pl.id WHERE p.match_id=$1 ORDER BY pl.name""", match_id)
    return {"hidden":False,"predictions":[dict(p) for p in preds],"match":dict(match)}

@app.get("/api/leaderboard")
async def leaderboard(token: str):
    await get_player_by_token(token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT pl.name,
                   COALESCE(SUM(p.points),0)+COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as total_points,
                   COUNT(p.id) as predictions_count,
                   SUM(CASE WHEN p.points>=3 THEN 1 ELSE 0 END) as exact_hits,
                   SUM(CASE WHEN p.points=1 THEN 1 ELSE 0 END) as outcome_hits,
                   CASE WHEN COUNT(p.id)>0 THEN ROUND(100.0*SUM(CASE WHEN p.points>0 THEN 1 ELSE 0 END)/COUNT(p.id),1) ELSE 0 END as hit_pct,
                   COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as tournament_bonus
            FROM players pl LEFT JOIN predictions p ON pl.id=p.player_id
            GROUP BY pl.id ORDER BY total_points DESC""")
    return [dict(r) for r in rows]

@app.get("/api/archive")
async def archive(token: str):
    await get_player_by_token(token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        matches = await conn.fetch("SELECT * FROM matches WHERE status='finished' ORDER BY match_time DESC")
        result = []
        for m in matches:
            preds = await conn.fetch("""SELECT pl.name,p.home_score,p.away_score,p.points,p.is_vabank
                FROM predictions p JOIN players pl ON p.player_id=pl.id
                WHERE p.match_id=$1 ORDER BY p.points DESC,pl.name""", m["id"])
            d = dict(m); d["predictions"] = [dict(p) for p in preds]
            result.append(d)
    return result

class TournamentPredIn(BaseModel):
    token: str; champion: str; finalist1: str; finalist2: str; top_scorer: str

@app.post("/api/tournament-prediction")
async def set_tournament_prediction(body: TournamentPredIn):
    player = await get_player_by_token(body.token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""INSERT INTO tournament_predictions (player_id,champion,finalist1,finalist2,top_scorer)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT(player_id) DO UPDATE SET
              champion=EXCLUDED.champion,finalist1=EXCLUDED.finalist1,
              finalist2=EXCLUDED.finalist2,top_scorer=EXCLUDED.top_scorer""",
            player["id"],body.champion,body.finalist1,body.finalist2,body.top_scorer)
    return {"ok":True}

@app.get("/api/tournament-prediction")
async def get_tournament_prediction(token: str):
    player = await get_player_by_token(token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM tournament_predictions WHERE player_id=$1", player["id"])
        result = await conn.fetchrow("SELECT * FROM tournament_result WHERE id=1")
    return {"my_prediction":dict(row) if row else None,"result":dict(result) if result else None}

@app.get("/api/tournament-predictions-all")
async def get_all_tournament_predictions(token: str):
    player = await get_player_by_token(token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        started = await conn.fetchval("SELECT COUNT(*) FROM matches WHERE status != 'upcoming'") > 0
        total_players = await conn.fetchval("SELECT COUNT(*) FROM players")
        done_players = await conn.fetchval("SELECT COUNT(*) FROM tournament_predictions")
        rows = await conn.fetch("""SELECT pl.name,tp.champion,tp.finalist1,tp.finalist2,tp.top_scorer,
                   tp.champion_pts,tp.finalist_pts,tp.scorer_pts,pl.id as player_id
            FROM tournament_predictions tp JOIN players pl ON tp.player_id=pl.id""")
        result = await conn.fetchrow("SELECT * FROM tournament_result WHERE id=1")
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
async def add_player(body: PlayerIn):
    token = secrets.token_urlsafe(16)
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("INSERT INTO players (name,telegram_chat_id,token) VALUES ($1,$2,$3)",
                              body.name,body.telegram_chat_id,token)
        except Exception:
            raise HTTPException(400,"Участник уже существует")
    return {"name":body.name,"token":token}

@app.get("/api/admin/players", dependencies=[Depends(require_admin)])
async def get_players():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id,name,telegram_chat_id,token FROM players")
    return [dict(r) for r in rows]

@app.post("/api/admin/matches", dependencies=[Depends(require_admin)])
async def add_match(body: MatchIn):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("INSERT INTO matches (home_team,away_team,match_time) VALUES ($1,$2,$3) RETURNING id",
                                 body.home_team,body.away_team,body.match_time)
    return {"id":row["id"]}

@app.post("/api/admin/matches/batch", dependencies=[Depends(require_admin)])
async def add_matches_batch(body: MatchBatchIn):
    added = 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        for m in body.matches:
            try:
                await conn.execute("INSERT INTO matches (home_team,away_team,match_time) VALUES ($1,$2,$3)",
                                  m.home_team,m.away_team,m.match_time)
                added += 1
            except Exception as e:
                print(f"Batch error: {e}")
    return {"added":added}

@app.delete("/api/admin/matches/{match_id}", dependencies=[Depends(require_admin)])
async def delete_match(match_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM predictions WHERE match_id=$1", match_id)
        await conn.execute("DELETE FROM matches WHERE id=$1", match_id)
    return {"ok":True}

@app.post("/api/admin/matches/{match_id}/result", dependencies=[Depends(require_admin)])
async def set_result(match_id: int, body: ResultIn):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE matches SET home_score=$1,away_score=$2,status='finished' WHERE id=$3",
                          body.home_score,body.away_score,match_id)
        preds = await conn.fetch("SELECT id,home_score,away_score,is_vabank FROM predictions WHERE match_id=$1", match_id)
        for p in preds:
            pts = calc_points(p["home_score"],p["away_score"],body.home_score,body.away_score,bool(p["is_vabank"]))
            await conn.execute("UPDATE predictions SET points=$1 WHERE id=$2", pts,p["id"])
    return {"ok":True}

@app.get("/api/admin/matches", dependencies=[Depends(require_admin)])
async def admin_matches():
    pool = await get_pool()
    async with pool.acquire() as conn:
        matches = await conn.fetch("SELECT * FROM matches ORDER BY match_time ASC")
        preds = await conn.fetch("""SELECT p.match_id,pl.name,p.home_score,p.away_score,p.points,p.is_vabank
            FROM predictions p JOIN players pl ON p.player_id=pl.id""")
    pred_map = {}
    for p in preds:
        pred_map.setdefault(p["match_id"],[]).append(dict(p))
    return [dict(m)|{"predictions":pred_map.get(m["id"],[])} for m in matches]

@app.post("/api/admin/tournament-result", dependencies=[Depends(require_admin)])
async def set_tournament_result(body: TournamentResultIn):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""INSERT INTO tournament_result (id,champion,finalist1,finalist2,top_scorer)
            VALUES (1,$1,$2,$3,$4)
            ON CONFLICT(id) DO UPDATE SET champion=EXCLUDED.champion,finalist1=EXCLUDED.finalist1,
              finalist2=EXCLUDED.finalist2,top_scorer=EXCLUDED.top_scorer""",
            body.champion,body.finalist1,body.finalist2,body.top_scorer)
        preds = await conn.fetch("SELECT * FROM tournament_predictions")
        for tp in preds:
            c = 10 if tp["champion"] and tp["champion"].lower().strip()==body.champion.lower().strip() else 0
            fins = {body.finalist1.lower().strip(),body.finalist2.lower().strip()}
            f = 0
            if tp["finalist1"] and tp["finalist1"].lower().strip() in fins: f+=5
            if tp["finalist2"] and tp["finalist2"].lower().strip() in fins: f+=5
            s = 10 if tp["top_scorer"] and tp["top_scorer"].lower().strip()==body.top_scorer.lower().strip() else 0
            await conn.execute("UPDATE tournament_predictions SET champion_pts=$1,finalist_pts=$2,scorer_pts=$3 WHERE player_id=$4",
                              c,f,s,tp["player_id"])
    return {"ok":True}

@app.post("/api/telegram/webhook")
async def telegram_webhook(update: dict):
    msg = update.get("message",{})
    text = msg.get("text","")
    chat_id = str(msg.get("chat",{}).get("id",""))
    if text.startswith("/start "):
        token = text.split(" ",1)[1].strip()
        pool = await get_pool()
        async with pool.acquire() as conn:
            player = await conn.fetchrow("SELECT * FROM players WHERE token=$1", token)
            if player:
                await conn.execute("UPDATE players SET telegram_chat_id=$1 WHERE token=$2", chat_id,token)
                await send_telegram(chat_id, f"✅ Привет, <b>{player['name']}</b>! Ты подключён к турниру прогнозов ⚽\nУдачи! 🏆")
            else:
                await send_telegram(chat_id, "❌ Неверный токен.")
    return {"ok":True}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
