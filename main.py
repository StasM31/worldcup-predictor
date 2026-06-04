from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
import sqlite3, os, httpx, asyncio, secrets
from contextlib import contextmanager
from datetime import datetime, timezone

app = FastAPI()
DATABASE = "predictor.db"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme123")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GRACE_SECONDS = 60

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
            champion TEXT,
            finalist1 TEXT,
            finalist2 TEXT,
            top_scorer TEXT,
            champion_pts INTEGER DEFAULT 0,
            finalist_pts INTEGER DEFAULT 0,
            scorer_pts INTEGER DEFAULT 0,
            FOREIGN KEY(player_id) REFERENCES players(id)
        );
        CREATE TABLE IF NOT EXISTS tournament_result (
            id INTEGER PRIMARY KEY CHECK (id=1),
            champion TEXT,
            finalist1 TEXT,
            finalist2 TEXT,
            top_scorer TEXT
        );
        """)
        # Миграция — добавить is_vabank если не существует
        try:
            db.execute("ALTER TABLE predictions ADD COLUMN is_vabank INTEGER DEFAULT 0")
        except:
            pass

init_db()

# ── Авто-старт ──
# ── Авто-старт ──
async def auto_start_scheduler():
    """Время матчей хранится как московское (UTC+3), сравниваем с текущим МСК."""
    from datetime import timedelta
    await asyncio.sleep(5)
    while True:
        try:
            now_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None)
            with get_db() as db:
                matches = db.execute("SELECT * FROM matches WHERE status='upcoming'").fetchall()
                for m in matches:
                    mt = parse_dt(m["match_time"])
                    if mt and now_msk >= mt.replace(tzinfo=None):
                        db.execute("UPDATE matches SET status='grace', started_at=? WHERE id=?",
                                   (datetime.now(timezone.utc).isoformat(), m["id"]))
                        asyncio.create_task(grace_period_end(m["id"]))
                        print(f"Auto-started: {m['home_team']} vs {m['away_team']}")
        except Exception as e:
            print(f"Scheduler error: {e}")
        await asyncio.sleep(30)

# ── Ежедневный дайджест в 18:00 МСК ──
DAILY_DIGEST_HOUR_MSK = 18

async def daily_digest_scheduler():
    """Каждый день в 18:00 МСК отправляет таблицу лидеров всем участникам."""
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
                print(f"Daily digest sent at {now_msk}")
        except Exception as e:
            print(f"Daily digest error: {e}")
        await asyncio.sleep(30)

async def send_daily_digest():
    """Формирует и рассылает таблицу лидеров."""
    with get_db() as db:
        rows = db.execute("""
            SELECT pl.name,
                   COALESCE(SUM(p.points),0) +
                   COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as total_points,
                   COUNT(p.id) as pred_count,
                   SUM(CASE WHEN p.points>=3 THEN 1 ELSE 0 END) as exact_hits,
                   SUM(CASE WHEN p.points=1 THEN 1 ELSE 0 END) as outcome_hits
            FROM players pl
            LEFT JOIN predictions p ON pl.id=p.player_id
            GROUP BY pl.id ORDER BY total_points DESC
        """).fetchall()
        players = db.execute("SELECT telegram_chat_id FROM players WHERE telegram_chat_id IS NOT NULL").fetchall()

    if not rows:
        return

    medals = ['🥇', '🥈', '🥉']
    from datetime import timedelta, date
    today_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime('%d.%m.%Y')

    lines = [f"📊 <b>Таблица лидеров на {today_msk}</b>\n"]
    for i, r in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(
            f"{medal} <b>{r['name']}</b> — <b>{r['total_points']} очк.</b>  "
            f"🎯{r['exact_hits']} ✅{r['outcome_hits']}"
        )

    text = "\n".join(lines)
    tasks = [send_telegram(str(pl["telegram_chat_id"]), text) for pl in players]
    if tasks:
        await asyncio.gather(*tasks)

@app.on_event("startup")
async def startup():
    asyncio.create_task(auto_start_scheduler())
    asyncio.create_task(daily_digest_scheduler())

# Ручная отправка дайджеста (для теста)
@app.post("/api/admin/send-digest", dependencies=[Depends(require_admin)])
async def manual_digest():
    await send_daily_digest()
    return {"ok": True}

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

# ── Telegram ──
# Флаги стран для Telegram (эмодзи региональных индикаторов)
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
    'южная африка':'🇿🇦','босния и герцеговина':'🇧🇦','босния':'🇧🇦',
    'др конго':'🇨🇩','конго':'🇨🇬','парагвай':'🇵🇾','армения':'🇦🇲',
    'азербайджан':'🇦🇿','грузия':'🇬🇪','панама':'🇵🇦','гондурас':'🇭🇳',
    'коста-рика':'🇨🇷','венесуэла':'🇻🇪','боливия':'🇧🇴',
    # EN
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
    'paraguay':'🇵🇾','armenia':'🇦🇲','georgia':'🇬🇪','scotland':'🏴󠁧󠁢󠁳󠁣󠁴󠁿',
}

def get_flag(name: str) -> str:
    if not name:
        return ''
    key = name.strip().lower()
    flag = TEAM_FLAGS.get(key, '')
    if not flag:
        for k, v in TEAM_FLAGS.items():
            if key in k or k in key:
                flag = v
                break
    return flag

def team_with_flag(name: str) -> str:
    flag = get_flag(name)
    return f"{name} {flag}" if flag else name

async def send_telegram(chat_id: str, text: str):
    if not BOT_TOKEN or not chat_id:
        return
    async with httpx.AsyncClient() as client:
        try:
            await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                              json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        except Exception as e:
            print(f"Telegram error: {e}")

async def broadcast_predictions(match_id: int):
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        preds = db.execute("""
            SELECT pl.name, p.home_score, p.away_score, p.is_vabank
            FROM predictions p JOIN players pl ON p.player_id=pl.id WHERE p.match_id=?
        """, (match_id,)).fetchall()
        all_players = db.execute("SELECT id, name, telegram_chat_id FROM players").fetchall()
    if not match:
        return
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

def check_and_broadcast(match_id: int):
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        done = db.execute("SELECT COUNT(*) FROM predictions WHERE match_id=?", (match_id,)).fetchone()[0]
    if total > 0 and done >= total:
        asyncio.create_task(broadcast_predictions(match_id))

async def grace_period_end(match_id: int):
    await asyncio.sleep(GRACE_SECONDS)
    with get_db() as db:
        m = db.execute("SELECT status FROM matches WHERE id=?", (match_id,)).fetchone()
        if m and m["status"] == "grace":
            db.execute("UPDATE matches SET status='live' WHERE id=?", (match_id,))
    await broadcast_predictions(match_id)
    # Запускаем авто-финиш через 120 минут после старта
    asyncio.create_task(auto_finish_match(match_id))

MATCH_DURATION_SECONDS = 120 * 60  # 120 минут

async def auto_finish_match(match_id: int):
    """Через 120 минут после старта переводим матч в статус ended."""
    await asyncio.sleep(MATCH_DURATION_SECONDS - GRACE_SECONDS)
    with get_db() as db:
        m = db.execute("SELECT status FROM matches WHERE id=?", (match_id,)).fetchone()
        if m and m["status"] == "live":
            db.execute("UPDATE matches SET status='ended' WHERE id=?", (match_id,))
            print(f"Auto-ended match {match_id}")

def calc_points(ph, pa, rh, ra, is_vabank=False):
    if ph == rh and pa == ra:
        return 9 if is_vabank else 3
    po = "H" if ph > pa else ("A" if ph < pa else "D")
    ro = "H" if rh > ra else ("A" if rh < ra else "D")
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
    with get_db() as db:
        vb = db.execute("SELECT 1 FROM vabank_used WHERE player_id=?", (player["id"],)).fetchone()
    return {"id": player["id"], "name": player["name"], "vabank_used": bool(vb)}

@app.get("/api/matches")
def list_matches(token: str):
    player = get_player_by_token(token)
    with get_db() as db:
        matches = db.execute("SELECT * FROM matches ORDER BY match_time ASC").fetchall()
        my_preds = db.execute(
            "SELECT match_id, home_score, away_score, is_vabank FROM predictions WHERE player_id=?",
            (player["id"],)).fetchall()
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
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not match:
            raise HTTPException(404, "Матч не найден")
        if match["status"] not in ("upcoming", "grace"):
            raise HTTPException(400, "Приём ставок закрыт")  # live, ended, finished
        existing = db.execute("SELECT id, is_vabank FROM predictions WHERE player_id=? AND match_id=?",
                      (player["id"], match_id)).fetchone()
        # В грейс-период менять нельзя — матч уже начался
        if existing and match["status"] == "grace":
            raise HTTPException(400, "Матч уже начался, прогноз менять нельзя")
        is_vabank = 0
        if body.is_vabank:
            # Проверяем ва-банк только если это новая ставка
            if not existing:
                if db.execute("SELECT 1 FROM vabank_used WHERE player_id=?", (player["id"],)).fetchone():
                    raise HTTPException(400, "Ва-банк уже использован в этом турнире")
                db.execute("INSERT OR IGNORE INTO vabank_used (player_id) VALUES (?)", (player["id"],))
            is_vabank = 1
        if existing:
            # Обновляем существующий прогноз (только если upcoming)
            # Если был ва-банк и снимаем — возвращаем возможность использовать
            if existing["is_vabank"] and not is_vabank:
                db.execute("DELETE FROM vabank_used WHERE player_id=?", (player["id"],))
            db.execute(
                "UPDATE predictions SET home_score=?, away_score=?, is_vabank=? WHERE player_id=? AND match_id=?",
                (body.home_score, body.away_score, is_vabank, player["id"], match_id))
        else:
            db.execute(
                "INSERT INTO predictions (player_id, match_id, home_score, away_score, is_vabank) VALUES (?,?,?,?,?)",
                (player["id"], match_id, body.home_score, body.away_score, is_vabank))
    check_and_broadcast(match_id)
    return {"ok": True}

@app.get("/api/match/{match_id}/predictions")
def match_predictions(match_id: int, token: str):
    get_player_by_token(token)
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not match:
            raise HTTPException(404)
        total = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        done = db.execute("SELECT COUNT(*) FROM predictions WHERE match_id=?", (match_id,)).fetchone()[0]
        # Скрываем прогнозы пока матч не начался (статус upcoming)
        if match["status"] == "upcoming":
            return {"hidden": True, "reason": f"Прогнозы скрыты до начала матча • {done}/{total} сделали ставку"}
        # Для ended тоже показываем прогнозы (матч завершён, ждём счёт)
        preds = db.execute("""
            SELECT pl.name, p.home_score, p.away_score, p.points, p.is_vabank
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
                   COALESCE(SUM(p.points),0) +
                   COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as total_points,
                   COALESCE(SUM(p.points),0) as match_points,
                   COUNT(p.id) as predictions_count,
                   SUM(CASE WHEN p.points>=3 AND p.is_vabank=0 THEN 1 WHEN p.points>=9 THEN 1 ELSE 0 END) as exact_hits,
                   SUM(CASE WHEN p.points=1 OR p.points=3 AND p.is_vabank=1 THEN 1 ELSE 0 END) as outcome_hits,
                   CASE WHEN COUNT(p.id)>0 THEN ROUND(100.0*(SUM(CASE WHEN p.points>0 THEN 1 ELSE 0 END))/COUNT(p.id),1) ELSE 0 END as hit_pct,
                   COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as tournament_bonus
            FROM players pl
            LEFT JOIN predictions p ON pl.id=p.player_id
            GROUP BY pl.id ORDER BY total_points DESC
        """).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/archive")
def archive(token: str):
    get_player_by_token(token)
    with get_db() as db:
        matches = db.execute(
            "SELECT * FROM matches WHERE status='finished' ORDER BY match_time DESC").fetchall()
        result = []
        for m in matches:
            preds = db.execute("""
                SELECT pl.name, p.home_score, p.away_score, p.points, p.is_vabank
                FROM predictions p JOIN players pl ON p.player_id=pl.id
                WHERE p.match_id=? ORDER BY p.points DESC, pl.name
            """, (m["id"],)).fetchall()
            d = dict(m)
            d["predictions"] = [dict(p) for p in preds]
            result.append(d)
    return result

# ── Tournament predictions ──
class TournamentPredIn(BaseModel):
    token: str
    champion: str
    finalist1: str
    finalist2: str
    top_scorer: str

@app.post("/api/tournament-prediction")
def set_tournament_prediction(body: TournamentPredIn):
    player = get_player_by_token(body.token)
    with get_db() as db:
        db.execute("""
            INSERT INTO tournament_predictions (player_id, champion, finalist1, finalist2, top_scorer)
            VALUES (?,?,?,?,?)
            ON CONFLICT(player_id) DO UPDATE SET
              champion=excluded.champion, finalist1=excluded.finalist1,
              finalist2=excluded.finalist2, top_scorer=excluded.top_scorer
        """, (player["id"], body.champion, body.finalist1, body.finalist2, body.top_scorer))
    return {"ok": True}

@app.get("/api/tournament-prediction")
def get_tournament_prediction(token: str):
    player = get_player_by_token(token)
    with get_db() as db:
        row = db.execute("SELECT * FROM tournament_predictions WHERE player_id=?",
                         (player["id"],)).fetchone()
        result = db.execute("SELECT * FROM tournament_result WHERE id=1").fetchone()
    return {
        "my_prediction": dict(row) if row else None,
        "result": dict(result) if result else None
    }

@app.get("/api/tournament-predictions-all")
def get_all_tournament_predictions(token: str):
    player = get_player_by_token(token)
    with get_db() as db:
        # Турнир начался если есть хотя бы один матч не в статусе upcoming
        tournament_started = db.execute(
            "SELECT COUNT(*) FROM matches WHERE status != 'upcoming'"
        ).fetchone()[0] > 0
        total_players = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        done_players = db.execute("SELECT COUNT(*) FROM tournament_predictions").fetchone()[0]
        rows = db.execute("""
            SELECT pl.name, tp.champion, tp.finalist1, tp.finalist2, tp.top_scorer,
                   tp.champion_pts, tp.finalist_pts, tp.scorer_pts, pl.id as player_id
            FROM tournament_predictions tp JOIN players pl ON tp.player_id=pl.id
        """).fetchall()
        result = db.execute("SELECT * FROM tournament_result WHERE id=1").fetchone()
    if tournament_started:
        # Турнир начался — показываем все прогнозы
        return {
            "predictions": [dict(r) for r in rows],
            "result": dict(result) if result else None,
            "tournament_started": True,
            "done_count": done_players,
            "total_count": total_players
        }
    else:
        # Турнир не начался — показываем только свой прогноз, остальные скрыты
        my_pred = next((dict(r) for r in rows if r["player_id"] == player["id"]), None)
        return {
            "predictions": [my_pred] if my_pred else [],
            "result": None,
            "tournament_started": False,
            "done_count": done_players,
            "total_count": total_players
        }

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

@app.post("/api/admin/matches", dependencies=[Depends(require_admin)])
def add_match(body: MatchIn):
    with get_db() as db:
        cur = db.execute("INSERT INTO matches (home_team, away_team, match_time) VALUES (?,?,?)",
                         (body.home_team, body.away_team, body.match_time))
    return {"id": cur.lastrowid}

@app.post("/api/admin/matches/batch", dependencies=[Depends(require_admin)])
def add_matches_batch(body: MatchBatchIn):
    added = 0
    with get_db() as db:
        for m in body.matches:
            try:
                db.execute("INSERT INTO matches (home_team, away_team, match_time) VALUES (?,?,?)",
                           (m.home_team, m.away_team, m.match_time))
                added += 1
            except Exception as e:
                print(f"Batch error: {e}")
    return {"added": added}

@app.delete("/api/admin/matches/{match_id}", dependencies=[Depends(require_admin)])
def delete_match(match_id: int):
    with get_db() as db:
        db.execute("DELETE FROM predictions WHERE match_id=?", (match_id,))
        db.execute("DELETE FROM matches WHERE id=?", (match_id,))
    return {"ok": True}

@app.post("/api/admin/matches/{match_id}/result", dependencies=[Depends(require_admin)])
def set_result(match_id: int, body: ResultIn):
    with get_db() as db:
        db.execute("UPDATE matches SET home_score=?, away_score=?, status='finished' WHERE id=?",
                   (body.home_score, body.away_score, match_id))
        preds = db.execute("SELECT id, home_score, away_score, is_vabank FROM predictions WHERE match_id=?",
                           (match_id,)).fetchall()
        for p in preds:
            pts = calc_points(p["home_score"], p["away_score"], body.home_score, body.away_score, bool(p["is_vabank"]))
            db.execute("UPDATE predictions SET points=? WHERE id=?", (pts, p["id"]))
    return {"ok": True}

@app.put("/api/admin/matches/{match_id}/status", dependencies=[Depends(require_admin)])
def update_match_status(match_id: int, status: str):
    if status not in ("upcoming", "live", "grace", "ended", "finished"):
        raise HTTPException(400, "Invalid status")
    with get_db() as db:
        db.execute("UPDATE matches SET status=? WHERE id=?", (status, match_id))
    return {"ok": True}

@app.get("/api/admin/matches", dependencies=[Depends(require_admin)])
def admin_matches():
    with get_db() as db:
        rows = db.execute("SELECT * FROM matches ORDER BY match_time ASC").fetchall()
        preds = db.execute("""
            SELECT p.match_id, pl.name, p.home_score, p.away_score, p.points, p.is_vabank
            FROM predictions p JOIN players pl ON p.player_id=pl.id
        """).fetchall()
    pred_map = {}
    for p in preds:
        pred_map.setdefault(p["match_id"], []).append(dict(p))
    return [dict(m) | {"predictions": pred_map.get(m["id"], [])} for m in rows]

@app.post("/api/admin/tournament-result", dependencies=[Depends(require_admin)])
def set_tournament_result(body: TournamentResultIn):
    with get_db() as db:
        db.execute("""
            INSERT INTO tournament_result (id, champion, finalist1, finalist2, top_scorer)
            VALUES (1,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              champion=excluded.champion, finalist1=excluded.finalist1,
              finalist2=excluded.finalist2, top_scorer=excluded.top_scorer
        """, (body.champion, body.finalist1, body.finalist2, body.top_scorer))
        # Пересчитать бонусы
        preds = db.execute("SELECT * FROM tournament_predictions").fetchall()
        for tp in preds:
            champ_pts = 10 if tp["champion"] and tp["champion"].lower().strip() == body.champion.lower().strip() else 0
            fin_pts = 0
            finalists = {body.finalist1.lower().strip(), body.finalist2.lower().strip()}
            if tp["finalist1"] and tp["finalist1"].lower().strip() in finalists:
                fin_pts += 5
            if tp["finalist2"] and tp["finalist2"].lower().strip() in finalists:
                fin_pts += 5
            scorer_pts = 10 if tp["top_scorer"] and tp["top_scorer"].lower().strip() == body.top_scorer.lower().strip() else 0
            db.execute("""
                UPDATE tournament_predictions SET champion_pts=?, finalist_pts=?, scorer_pts=?
                WHERE player_id=?
            """, (champ_pts, fin_pts, scorer_pts, tp["player_id"]))
    return {"ok": True}

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
                await send_telegram(chat_id,
                    f"✅ Привет, <b>{player['name']}</b>! Ты подключён к турниру прогнозов ⚽\nТеперь будешь получать уведомления перед матчами. Удачи! 🏆")
            else:
                await send_telegram(chat_id, "❌ Неверный токен.")
    return {"ok": True}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
