from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
import sqlite3, os, httpx, asyncio, secrets, html
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

app = FastAPI()

# Храним базу в /app/data/ если папка существует (Railway Volume), иначе рядом с кодом
_DATA_DIR = "/app/data" if os.path.isdir("/app/data") else "."
os.makedirs(_DATA_DIR, exist_ok=True)
DATABASE = os.path.join(_DATA_DIR, "predictor.db")

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme123")
if ADMIN_TOKEN == "changeme123":
    print("=" * 60)
    print("WARNING: ADMIN_TOKEN не задан в переменных окружения!")
    print("Используется небезопасный дефолт 'changeme123'.")
    print("Задайте ADMIN_TOKEN в настройках Railway как можно скорее.")
    print("=" * 60)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BACKUP_ADMIN_CHAT_ID = os.environ.get("BACKUP_CHAT_ID", "")
# Опционально: секрет вебхука Telegram. Если задан, вебхук принимает только
# запросы с заголовком X-Telegram-Bot-Api-Secret-Token (нужно перерегистрировать
# вебхук через setup_webhook.py после установки переменной).
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
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
            token TEXT UNIQUE NOT NULL,
            last_seen TEXT,
            is_guest INTEGER DEFAULT 0
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
        CREATE TABLE IF NOT EXISTS tournament_settings (
            id INTEGER PRIMARY KEY CHECK (id=1),
            entry_fee INTEGER DEFAULT 15000,
            prize_config TEXT DEFAULT '60,30,10',
            hide_days INTEGER DEFAULT 0
        );
        INSERT OR IGNORE INTO tournament_settings (id) VALUES (1);
        """)
        try:
            db.execute("ALTER TABLE predictions ADD COLUMN is_vabank INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # колонка уже существует

init_db()
# Migration: add hide_days if missing
try:
    with get_db() as _db:
        _db.execute("ALTER TABLE tournament_settings ADD COLUMN hide_days INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass  # колонка уже существует
# Migration: add last_seen if missing
try:
    with get_db() as _db:
        _db.execute("ALTER TABLE players ADD COLUMN last_seen TEXT")
except sqlite3.OperationalError:
    pass  # колонка уже существует
# Migration: add is_guest if missing
try:
    with get_db() as _db:
        _db.execute("ALTER TABLE players ADD COLUMN is_guest INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass  # колонка уже существует
try:
    with get_db() as _db:
        _db.execute("ALTER TABLE tournament_settings ADD COLUMN digest_enabled INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass  # колонка уже существует
try:
    with get_db() as _db:
        _db.execute("ALTER TABLE tournament_settings ADD COLUMN digest_time TEXT DEFAULT '18:00'")
except sqlite3.OperationalError:
    pass  # колонка уже существует
try:
    with get_db() as _db:
        _db.execute("ALTER TABLE tournament_settings ADD COLUMN welcome_enabled INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
try:
    with get_db() as _db:
        _db.execute("ALTER TABLE tournament_settings ADD COLUMN welcome_time TEXT DEFAULT ''")
except sqlite3.OperationalError:
    pass
try:
    with get_db() as _db:
        _db.execute("ALTER TABLE tournament_settings ADD COLUMN welcome_sent INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
# Migration: add stage (стадия матча: 1/16, финал и т.д.) if missing
try:
    with get_db() as _db:
        _db.execute("ALTER TABLE matches ADD COLUMN stage TEXT")
except sqlite3.OperationalError:
    pass  # колонка уже существует
# Migration: track which match a va-bank was placed on (для корректного учёта)
try:
    with get_db() as _db:
        _db.execute("ALTER TABLE vabank_used ADD COLUMN match_id INTEGER")
except sqlite3.OperationalError:
    pass  # колонка уже существует

def _normalize_vabank_data():
    """Одноразовая починка данных после бага, при котором ва-банк, поставленный
    поверх уже существующего прогноза, не отмечался в vabank_used. Из-за этого у
    некоторых игроков могло оказаться несколько ва-банков. Оставляем один:
    приоритет у уже сыгранного матча (там очки посчитаны), иначе — самый ранний.
    Функция идемпотентна: при отсутствии дублей ничего не меняет."""
    try:
        with get_db() as db:
            players = db.execute("SELECT DISTINCT player_id FROM predictions WHERE is_vabank=1").fetchall()
            for row in players:
                pid = row["player_id"]
                vbs = db.execute("""
                    SELECT p.match_id, m.status, m.match_time
                    FROM predictions p JOIN matches m ON m.id=p.match_id
                    WHERE p.player_id=? AND p.is_vabank=1
                    ORDER BY (m.status IN ('finished','ended','live')) DESC, m.match_time ASC
                """, (pid,)).fetchall()
                if not vbs:
                    continue
                keep = vbs[0]["match_id"]
                extra = [v["match_id"] for v in vbs[1:]]
                if extra:
                    # Снимаем лишние ва-банки только с НЕ сыгранных матчей, чтобы не трогать очки
                    for mid in extra:
                        st = next(v["status"] for v in vbs if v["match_id"]==mid)
                        if st in ("finished","ended","live"):
                            # конфликт: два ва-банка на сыгранных матчах — оставляем как есть, но логируем
                            print(f"[vabank-fix] ВНИМАНИЕ: игрок {pid} имеет ва-банк на сыгранном матче {mid}, требуется ручная проверка")
                            continue
                        db.execute("UPDATE predictions SET is_vabank=0 WHERE player_id=? AND match_id=?", (pid, mid))
                        print(f"[vabank-fix] игрок {pid}: снят лишний ва-банк с матча {mid}")
                # Синхронизируем vabank_used с оставленным матчем
                db.execute("INSERT OR REPLACE INTO vabank_used (player_id, match_id) VALUES (?,?)", (pid, keep))
            # Чистим записи vabank_used у игроков, у которых по факту нет ни одного ва-банка
            db.execute("""DELETE FROM vabank_used WHERE player_id NOT IN
                          (SELECT DISTINCT player_id FROM predictions WHERE is_vabank=1)""")
    except Exception as e:
        print(f"[vabank-fix] ошибка нормализации: {e}")

_normalize_vabank_data()

def parse_dt(s):
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

def require_admin(x_admin_token: str = Header(...)):
    if not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
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

def esc(s):
    """Экранирует <, >, & для Telegram parse_mode=HTML."""
    return html.escape(str(s), quote=False)

def team_with_flag(name):
    f = get_flag_emoji(name)
    return f"{esc(name)} {f}" if f else esc(name)

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
            FROM predictions p JOIN players pl ON p.player_id=pl.id
            WHERE p.match_id=? AND (pl.is_guest=0 OR pl.is_guest IS NULL)""", (match_id,)).fetchall()
        all_players = db.execute("SELECT id,name,telegram_chat_id FROM players WHERE is_guest=0 OR is_guest IS NULL").fetchall()
    if not match: return
    pred_map = {p["name"]: p for p in preds}
    home = team_with_flag(match['home_team'])
    away = team_with_flag(match['away_team'])
    lines = [f"⚽ <b>{home} vs {away}</b>\n"]
    for pl in sorted(all_players, key=lambda x: x["name"]):
        p = pred_map.get(pl["name"])
        if p:
            vb = " 🔥<b>ВА-БАНК</b>" if p["is_vabank"] else ""
            lines.append(f"• {esc(pl['name'])}: <b>{p['home_score']}:{p['away_score']}</b>{vb}")
        else:
            lines.append(f"• {esc(pl['name'])}: 😴 нет прогноза")
    text = "\n".join(lines)
    tasks = [send_telegram(str(pl["telegram_chat_id"]), text) for pl in all_players if pl["telegram_chat_id"]]
    if tasks: await asyncio.gather(*tasks)

def check_and_broadcast(match_id):
    with get_db() as db:
        match = db.execute("SELECT status FROM matches WHERE id=?", (match_id,)).fetchone()
        # Рассылаем только если матч уже начался (grace или live)
        if not match or match["status"] not in ("grace", "live"):
            return
        total = db.execute("SELECT COUNT(*) FROM players WHERE is_guest=0 OR is_guest IS NULL").fetchone()[0]
        done = db.execute("SELECT COUNT(*) FROM predictions WHERE match_id=?", (match_id,)).fetchone()[0]
    if total > 0 and done >= total:
        asyncio.create_task(broadcast_predictions(match_id))

async def grace_period_end(match_id, delay=GRACE_SECONDS):
    await asyncio.sleep(delay)
    with get_db() as db:
        m = db.execute("SELECT status FROM matches WHERE id=?", (match_id,)).fetchone()
        if m and m["status"] == "grace":
            db.execute("UPDATE matches SET status='live' WHERE id=?", (match_id,))
    await broadcast_predictions(match_id)
    asyncio.create_task(auto_finish_match(match_id))
    # Если это первый матч — рассылаем долгосрочные прогнозы
    with get_db() as db:
        live_count = db.execute(
            "SELECT COUNT(*) FROM matches WHERE status IN ('live','ended','finished')"
        ).fetchone()[0]
    if live_count == 1:
        asyncio.create_task(send_longterm_after_start())

async def send_longterm_after_start():
    """Отправляет долгосрочные прогнозы сразу после старта первого матча."""
    await asyncio.sleep(5)
    with get_db() as db:
        players = db.execute("SELECT * FROM players WHERE is_guest=0 OR is_guest IS NULL").fetchall()
        n = len(players)
        preds = db.execute("""
            SELECT pl.name, tp.champion, tp.finalist1, tp.finalist2, tp.top_scorer
            FROM tournament_predictions tp
            JOIN players pl ON tp.player_id = pl.id
            WHERE pl.is_guest=0 OR pl.is_guest IS NULL
            ORDER BY pl.name
        """).fetchall()
    if not preds:
        return
    lines = [
        "🌍 <b>Долгосрочные прогнозы участников</b>",
        "Чемпионат мира 2026 · Ставки закрыты",
        "",
        "🥇 <b>Чемпион:</b>"
    ]
    for p in preds:
        f = get_flag_emoji(p['champion']) if p['champion'] else ''
        lines.append(f"  • {esc(p['name'])} → {f} {esc(p['champion']) if p['champion'] else '—'}")
    lines.extend(["", "🥈 <b>Финалисты:</b>"])
    for p in preds:
        f1 = get_flag_emoji(p['finalist1']) if p['finalist1'] else ''
        f2 = get_flag_emoji(p['finalist2']) if p['finalist2'] else ''
        fin1 = f"{f1} {esc(p['finalist1'])}" if p['finalist1'] else '—'
        fin2 = f"{f2} {esc(p['finalist2'])}" if p['finalist2'] else '—'
        lines.append(f"  • {esc(p['name'])} → {fin1} / {fin2}")
    lines.extend(["", "🥉 <b>3-е место:</b>"])
    for p in preds:
        f = get_flag_emoji(p['top_scorer']) if p['top_scorer'] else ''
        lines.append(f"  • {esc(p['name'])} → {f} {esc(p['top_scorer']) if p['top_scorer'] else '—'}")
    text = "\n".join(lines)
    tasks = [send_telegram(str(pl["telegram_chat_id"]), text) for pl in players if pl["telegram_chat_id"]]
    if tasks:
        await asyncio.gather(*tasks)
    print("Long-term predictions broadcast sent!")

async def auto_finish_match(match_id, delay=MATCH_DURATION_SECONDS - GRACE_SECONDS):
    await asyncio.sleep(delay)
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

def _parse_started_at(s):
    if not s: return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None

async def recover_inflight_matches():
    """После рестарта сервера восстанавливает таймеры матчей, застрявших в grace/live.

    Таймеры (grace_period_end, auto_finish_match) живут в памяти и теряются
    при редеплое. Если статус всё ещё 'grace' — рассылка прогнозов не успела
    уйти (grace_period_end сначала меняет статус на 'live', потом рассылает),
    поэтому безопасно дозапустить grace_period_end с остатком времени.
    Если статус 'live' — рассылка уже была, дозапускаем только авто-завершение.
    """
    await asyncio.sleep(2)
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT id, status, started_at FROM matches WHERE status IN ('grace','live')"
            ).fetchall()
        if not rows: return
        now = datetime.now(timezone.utc)
        for m in rows:
            started = _parse_started_at(m["started_at"])
            elapsed = (now - started).total_seconds() if started else MATCH_DURATION_SECONDS
            if m["status"] == "grace":
                remaining = max(0, GRACE_SECONDS - elapsed)
                asyncio.create_task(grace_period_end(m["id"], delay=remaining))
                print(f"Recovery: match {m['id']} in grace, resuming in {remaining:.0f}s")
            else:  # live
                remaining = max(0, MATCH_DURATION_SECONDS - elapsed)
                asyncio.create_task(auto_finish_match(m["id"], delay=remaining))
                print(f"Recovery: match {m['id']} live, auto-finish in {remaining:.0f}s")
    except Exception as e:
        print(f"Recovery error: {e}")

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

async def tourn_pred_reminder_scheduler():
    """В 19:00 МСК (за 3 часа до первого матча) напоминает тем, кто не сделал ставку на финал."""
    await asyncio.sleep(30)
    sent = False
    while True:
        try:
            if not sent:
                now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
                with get_db() as db:
                    first = db.execute(
                        "SELECT match_time FROM matches WHERE status='upcoming' ORDER BY match_time ASC LIMIT 1"
                    ).fetchone()
                if first:
                    mt = parse_dt(first["match_time"])
                    # Отправляем когда наступило 19:00 МСК в день первого матча (но до его старта)
                    if mt and now_msk.strftime("%Y-%m-%d") == mt.strftime("%Y-%m-%d") \
                       and now_msk.hour >= 19 \
                       and now_msk.replace(tzinfo=None) < mt.replace(tzinfo=None):
                        sent = True
                        await send_tourn_pred_reminder()
        except Exception as e:
            print(f"Tourn reminder scheduler error: {e}")
        await asyncio.sleep(60)

async def send_tourn_pred_reminder():
    """Напоминание только тем, у кого нет долгосрочного прогноза."""
    with get_db() as db:
        players = db.execute(
            "SELECT p.* FROM players p "
            "LEFT JOIN tournament_predictions tp ON p.id = tp.player_id "
            "WHERE tp.player_id IS NULL AND p.telegram_chat_id IS NOT NULL"
        ).fetchall()
    if not players:
        print("Tourn reminder: everyone has predictions, skipping")
        return
    text = "\n".join([
        "⏰ <b>Напоминание!</b>",
        "",
        "Ты ещё не сделал <b>долгосрочный прогноз на турнир</b> 🏆",
        "",
        "Зайди на вкладку <b>«Ставка на финал»</b> и угадай:",
        "  🥇 Чемпиона (+10 очков)",
        "  🥈 Финалистов (+5 за каждого)",
        "  🥉 Команду на 3-м месте (+5 очков)",
        "",
        "⚽ Первый матч сегодня в 22:00 МСК — после его старта ставка закроется НАВСЕГДА!",
    ])
    tasks = [send_telegram(str(pl["telegram_chat_id"]), text) for pl in players]
    await asyncio.gather(*tasks)
    print(f"Tourn reminder sent to {len(players)} players")

async def welcome_broadcast_scheduler():
    """Отправляет приветственное сообщение один раз в заданное админом время."""
    from datetime import timedelta
    await asyncio.sleep(20)
    while True:
        try:
            with get_db() as db:
                s = db.execute(
                    "SELECT welcome_enabled, welcome_time, welcome_sent FROM tournament_settings WHERE id=1"
                ).fetchone()
            enabled = s["welcome_enabled"] if s else 0
            already_sent = s["welcome_sent"] if s else 0
            welcome_time = (s["welcome_time"] if s and s["welcome_time"] else "").strip()
            if enabled and not already_sent and welcome_time:
                parts = welcome_time.split(":")
                target_h, target_m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
                now_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None)
                if now_msk.hour == target_h and now_msk.minute < 2:
                    with get_db() as db:
                        db.execute("UPDATE tournament_settings SET welcome_sent=1 WHERE id=1")
                    await send_welcome_broadcast()
                    print("Welcome broadcast sent (scheduled)")
        except Exception as e:
            print(f"Welcome scheduler error: {e}")
        await asyncio.sleep(30)

async def send_welcome_broadcast():
    with get_db() as db:
        players = db.execute("SELECT * FROM players WHERE is_guest=0 OR is_guest IS NULL").fetchall()
        settings = db.execute("SELECT * FROM tournament_settings WHERE id=1").fetchone()
        first = db.execute("SELECT * FROM matches ORDER BY match_time ASC LIMIT 1").fetchone()
    n = len(players)
    fee = settings["entry_fee"] if settings else 15000
    fund = n * fee
    raw_conf = settings["prize_config"] if settings else "60,30,10"
    if raw_conf.startswith("rub:"):
        prizes = [f"{PLACE_MEDALS[i]} {int(x):,} ₽".replace(",", " ") for i,x in enumerate(raw_conf.replace("rub:","").split(",")[:4])]
    else:
        prizes = [f"{PLACE_MEDALS[i]} {int(fund*float(p)/100):,} ₽".replace(",", " ") for i,p in enumerate(raw_conf.split(",")[:4])]
    home = team_with_flag(first["home_team"]) if first else ""
    away = team_with_flag(first["away_team"]) if first else ""

    # Сообщение 1 — Приветственное
    msg1_lines = [
        "🏆 <b>Турнир прогнозов ЧМ 2026 начинается!</b>",
        "",
        f"👥 Участников: <b>{n}</b>",
        f"💰 Призовой фонд: <b>{fund:,} ₽</b>".replace(",", " "),
        "",
        "🎁 Распределение призов:",
        *[f"  {p}" for p in prizes],
        "",
        f"⚽ Первый матч через <b>1 час</b>: {home} vs {away}",
        "",
        "🌍 <b>Не забудь сделать долгосрочный прогноз!</b>",
        "Зайди на вкладку «Ставка на финал» — это можно сделать только в течение следующего часа!",
        "",
        "🍀 Удачи всем! Пусть победит сильнейший!"
    ]
    text1 = "\n".join(msg1_lines)

    tasks = [send_telegram(str(pl["telegram_chat_id"]), text1) for pl in players if pl["telegram_chat_id"]]
    if tasks:
        await asyncio.gather(*tasks)
    print("Welcome broadcast sent!")

PLACE_MEDALS = ['🥇','🥈','🥉','4️⃣','5️⃣']

async def daily_digest_scheduler():
    from datetime import timedelta
    await asyncio.sleep(10)
    last_sent = None
    while True:
        try:
            with get_db() as db:
                s = db.execute("SELECT digest_enabled, digest_time FROM tournament_settings WHERE id=1").fetchone()
            enabled = s["digest_enabled"] if s else 0
            digest_time = (s["digest_time"] if s and s["digest_time"] else "18:00").strip()
            if enabled:
                parts = digest_time.split(":")
                target_h, target_m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
                now_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None)
                today = now_msk.date()
                if now_msk.hour == target_h and now_msk.minute < 2 and last_sent != today:
                    last_sent = today
                    await send_daily_digest()
        except Exception as e:
            print(f"Digest error: {e}")
        await asyncio.sleep(30)

async def tourn_reminder_scheduler():
    """Напоминание о ставке на финал в 20:00 МСК — только тем, кто не сделал."""
    from datetime import timedelta
    await asyncio.sleep(15)
    last_sent = None
    while True:
        try:
            now_msk = (datetime.now(timezone.utc)+timedelta(hours=3)).replace(tzinfo=None)
            today = now_msk.date()
            if now_msk.hour==20 and now_msk.minute<1 and last_sent!=today:
                # Проверяем есть ли те кто не сделал прогноз и турнир ещё не начался
                with get_db() as db:
                    pending = db.execute(
                        "SELECT p.* FROM players p "
                        "LEFT JOIN tournament_predictions tp ON p.id=tp.player_id "
                        "WHERE tp.player_id IS NULL AND p.telegram_chat_id IS NOT NULL"
                    ).fetchall()
                    not_started = db.execute(
                        "SELECT COUNT(*) FROM matches WHERE status != 'upcoming'"
                    ).fetchone()[0] == 0
                if pending and not_started:
                    last_sent = today
                    first = None
                    with get_db() as db:
                        first = db.execute(
                            "SELECT * FROM matches ORDER BY match_time ASC LIMIT 1"
                        ).fetchone()
                    when = ""
                    if first:
                        mt = parse_dt(first["match_time"])
                        if mt:
                            when = f" сегодня в {mt.strftime('%H:%M')} МСК"
                    text = (
                        "⏰ <b>Напоминание!</b>\n\n"
                        "Ты ещё не сделал <b>долгосрочный прогноз</b> на турнир!\n\n"
                        "🏆 Чемпион — +10 очков\n"
                        "🥈 Финалисты — +5 очков за каждого\n"
                        "🥉 3-е место — +5 очков\n\n"
                        f"⚽ Первый матч начнётся{when} — после его старта ставка на финал закроется <b>навсегда</b>!\n\n"
                        "Зайди на сайт → вкладка «Ставка на финал» 🌍"
                    )
                    tasks = [send_telegram(str(p["telegram_chat_id"]), text) for p in pending]
                    await asyncio.gather(*tasks)
                    print(f"Tourn reminder sent to {len(pending)} players")
                elif not not_started:
                    last_sent = today  # турнир уже начался — больше не напоминаем
        except Exception as e:
            print(f"Tourn reminder error: {e}")
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
            WHERE pl.is_guest=0 OR pl.is_guest IS NULL
            GROUP BY pl.id ORDER BY total_points DESC, pl.name ASC""").fetchall()
        players = db.execute("SELECT telegram_chat_id FROM players WHERE telegram_chat_id IS NOT NULL AND (is_guest IS NULL OR is_guest=0)").fetchall()
    if not rows: return
    medals = ['🥇','🥈','🥉']
    today_msk = (datetime.now(timezone.utc)+timedelta(hours=3)).strftime('%d.%m.%Y')
    lines = [f"📊 <b>Таблица лидеров на {today_msk}</b>\n"]
    for i,r in enumerate(rows):
        medal = medals[i] if i<3 else f"{i+1}."
        lines.append(f"{medal} <b>{esc(r['name'])}</b> — <b>{r['total_points']} очк.</b>")
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
    tmp_path = DATABASE + ".backup_tmp"
    try:
        from datetime import timedelta
        now_msk = (datetime.now(timezone.utc)+timedelta(hours=3)).strftime("%d.%m.%Y_%H-%M")
        # Снимаем целостную копию через SQLite backup API
        # (прямая отправка живого файла может дать битую копию при записи в этот момент)
        src = sqlite3.connect(DATABASE)
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)
        finally:
            dst.close(); src.close()
        async with httpx.AsyncClient() as client:
            with open(tmp_path, "rb") as f:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                    data={"chat_id":chat_id,"caption":f"🗄 Бэкап базы данных\n📅 {now_msk} МСК"},
                    files={"document":(f"backup_{now_msk}.db",f,"application/octet-stream")},
                    timeout=30)
    except Exception as e:
        print(f"Backup error: {e}")
    finally:
        try:
            if os.path.exists(tmp_path): os.remove(tmp_path)
        except OSError:
            pass

@app.on_event("startup")
async def startup():
    asyncio.create_task(recover_inflight_matches())
    asyncio.create_task(auto_start_scheduler())
    asyncio.create_task(daily_digest_scheduler())
    asyncio.create_task(daily_backup_scheduler())
    asyncio.create_task(welcome_broadcast_scheduler())
    asyncio.create_task(tourn_reminder_scheduler())
    asyncio.create_task(tourn_pred_reminder_scheduler())

@app.post("/api/admin/send-digest", dependencies=[Depends(require_admin)])
async def manual_digest():
    await send_daily_digest(); return {"ok":True}

@app.get("/api/admin/digest-settings", dependencies=[Depends(require_admin)])
def get_digest_settings():
    with get_db() as db:
        s = db.execute("SELECT digest_enabled, digest_time FROM tournament_settings WHERE id=1").fetchone()
    return {"enabled": bool(s["digest_enabled"]) if s else False,
            "time": s["digest_time"] if s and s["digest_time"] else "18:00"}

@app.post("/api/admin/digest-settings", dependencies=[Depends(require_admin)])
def set_digest_settings(body: dict):
    enabled = 1 if body.get("enabled") else 0
    digest_time = body.get("time", "18:00").strip()
    # Валидация формата HH:MM
    import re
    if not re.match(r"^\d{1,2}:\d{2}$", digest_time):
        raise HTTPException(400, "Формат времени: HH:MM")
    h, m = map(int, digest_time.split(":"))
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise HTTPException(400, "Некорректное время")
    with get_db() as db:
        db.execute("UPDATE tournament_settings SET digest_enabled=?, digest_time=? WHERE id=1",
                   (enabled, digest_time))
    return {"ok": True, "enabled": bool(enabled), "time": digest_time}

@app.get("/api/admin/welcome-settings", dependencies=[Depends(require_admin)])
def get_welcome_settings():
    with get_db() as db:
        s = db.execute("SELECT welcome_enabled, welcome_time, welcome_sent FROM tournament_settings WHERE id=1").fetchone()
    return {"enabled": bool(s["welcome_enabled"]) if s else False,
            "time": s["welcome_time"] if s and s["welcome_time"] else "",
            "sent": bool(s["welcome_sent"]) if s else False}

@app.post("/api/admin/welcome-settings", dependencies=[Depends(require_admin)])
def set_welcome_settings(body: dict):
    enabled = 1 if body.get("enabled") else 0
    welcome_time = body.get("time", "").strip()
    if welcome_time:
        import re
        if not re.match(r"^\d{1,2}:\d{2}$", welcome_time):
            raise HTTPException(400, "Формат времени: HH:MM")
        h, m = map(int, welcome_time.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise HTTPException(400, "Некорректное время")
    with get_db() as db:
        db.execute("UPDATE tournament_settings SET welcome_enabled=?, welcome_time=? WHERE id=1",
                   (enabled, welcome_time))
    return {"ok": True, "enabled": bool(enabled), "time": welcome_time}

@app.post("/api/admin/welcome-reset", dependencies=[Depends(require_admin)])
def reset_welcome_sent():
    """Сбрасывает флаг 'отправлено' — позволяет отправить приветствие повторно."""
    with get_db() as db:
        db.execute("UPDATE tournament_settings SET welcome_sent=0 WHERE id=1")
    return {"ok": True}

@app.post("/api/admin/send-welcome", dependencies=[Depends(require_admin)])
async def manual_welcome():
    with get_db() as db:
        db.execute("UPDATE tournament_settings SET welcome_sent=1 WHERE id=1")
    await send_welcome_broadcast()
    return {"ok": True}

@app.post("/api/admin/send-backup", dependencies=[Depends(require_admin)])
async def manual_backup():
    await send_backup(); return {"ok":True}

@app.post("/api/admin/send-custom", dependencies=[Depends(require_admin)])
async def send_custom_broadcast(body: dict):
    text = body.get("text","")
    if not text: raise HTTPException(400,"Текст не может быть пустым")
    with get_db() as db:
        players = db.execute("SELECT * FROM players WHERE (is_guest=0 OR is_guest IS NULL) AND telegram_chat_id IS NOT NULL").fetchall()
    tasks = [send_telegram(str(p["telegram_chat_id"]), text) for p in players]
    if tasks: await asyncio.gather(*tasks)
    return {"ok": True, "sent": len(players)}

@app.post("/api/admin/send-tourn-reminder", dependencies=[Depends(require_admin)])
async def send_tourn_reminder():
    """Напоминание о ставке на финал — только тем, кто её не сделал."""
    with get_db() as db:
        players = db.execute(
            "SELECT p.* FROM players p "
            "LEFT JOIN tournament_predictions tp ON p.id = tp.player_id "
            "WHERE tp.player_id IS NULL AND p.telegram_chat_id IS NOT NULL"
        ).fetchall()
        first = db.execute("SELECT * FROM matches WHERE status='upcoming' ORDER BY match_time ASC LIMIT 1").fetchone()
    if not players:
        return {"ok": True, "sent": 0}
    when = ""
    if first:
        mt = parse_dt(first["match_time"])
        if mt:
            when = f" сегодня в {mt.strftime('%H:%M')} МСК"
    text = (
        "⏰ <b>Напоминание!</b>\n\n"
        "Ты ещё не сделал <b>долгосрочный прогноз</b> на турнир!\n\n"
        "🏆 Чемпион — +10 очков\n"
        "🥈 Финалисты — +5 очков за каждого\n"
        "🥉 3-е место — +5 очков\n\n"
        f"⚽ Первый матч начнётся{when} — после его старта ставка на финал закроется <b>навсегда</b>!\n\n"
        "Зайди на сайт → вкладка «Ставка на финал» 🌍"
    )
    tasks = [send_telegram(str(p["telegram_chat_id"]), text) for p in players]
    await asyncio.gather(*tasks)
    return {"ok": True, "sent": len(players)}

# ── Player endpoints ──
class PredictionIn(BaseModel):
    token: str; home_score: int; away_score: int; is_vabank: bool = False

@app.get("/api/bot-info")
async def bot_info():
    """Возвращает username бота для формирования ссылки подключения."""
    if not BOT_TOKEN:
        return {"username": None}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=5)
            data = r.json()
            return {"username": data.get("result", {}).get("username")}
    except Exception:
        return {"username": None}

@app.get("/api/me")
def get_me(token: str):
    player = get_player_by_token(token)
    from datetime import timedelta
    with get_db() as db:
        vb = db.execute("SELECT 1 FROM vabank_used WHERE player_id=?", (player["id"],)).fetchone()
        now_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%d.%m %H:%M")
        db.execute("UPDATE players SET last_seen=? WHERE id=?", (now_msk, player["id"]))
    return {
        "id": player["id"],
        "name": player["name"],
        "vabank_used": bool(vb),
        "telegram_connected": bool(player["telegram_chat_id"]),
        "player_token": player["token"],
        "is_guest": bool(player["is_guest"])
    }

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
        already_vabank_here = bool(existing and existing["is_vabank"])
        is_vabank = 0
        if body.is_vabank:
            # Разрешаем ва-банк, только если он ещё не занят ДРУГИМ матчем.
            if not already_vabank_here:
                used = db.execute("SELECT match_id FROM vabank_used WHERE player_id=?", (player["id"],)).fetchone()
                if used and used["match_id"] is not None and used["match_id"] != match_id:
                    raise HTTPException(400,"Ва-банк уже использован на другом матче")
                # Подстраховка: снимаем флаг ва-банка со всех прочих матчей игрока
                db.execute("UPDATE predictions SET is_vabank=0 WHERE player_id=? AND match_id!=?", (player["id"], match_id))
            db.execute("INSERT OR REPLACE INTO vabank_used (player_id, match_id) VALUES (?,?)", (player["id"], match_id))
            is_vabank = 1
        else:
            # Игрок снимает ва-банк с этого матча — освобождаем его.
            if already_vabank_here:
                db.execute("DELETE FROM vabank_used WHERE player_id=? AND match_id=?", (player["id"], match_id))
        if existing:
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
        total = db.execute("SELECT COUNT(*) FROM players WHERE is_guest=0 OR is_guest IS NULL").fetchone()[0]
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
                   SUM(CASE WHEN p.points>0 THEN 1 ELSE 0 END) as outcome_hits,
                   CASE WHEN COUNT(p.id)>0 THEN ROUND(100.0*SUM(CASE WHEN p.points>0 THEN 1 ELSE 0 END)/COUNT(p.id),1) ELSE 0 END as hit_pct,
                   COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as tournament_bonus
            FROM players pl LEFT JOIN predictions p ON pl.id=p.player_id
            WHERE pl.is_guest=0 OR pl.is_guest IS NULL
            GROUP BY pl.id ORDER BY total_points DESC, pl.name ASC""").fetchall()
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

@app.get("/api/player-history")
def player_history(token: str, player_name: str):
    get_player_by_token(token)
    with get_db() as db:
        target = db.execute("SELECT id FROM players WHERE name=?", (player_name,)).fetchone()
        if not target:
            raise HTTPException(404, "Участник не найден")
        rows = db.execute("""
            SELECT m.home_team, m.away_team, m.match_time, m.home_score as real_home,
                   m.away_score as real_away, p.home_score, p.away_score, p.points, p.is_vabank
            FROM predictions p
            JOIN matches m ON p.match_id = m.id
            WHERE p.player_id = ? AND m.status = 'finished'
            ORDER BY m.match_time DESC
        """, (target["id"],)).fetchall()
    return [dict(r) for r in rows]

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
        first_mt = db.execute("SELECT MIN(match_time) FROM matches").fetchone()[0]
        total_players = db.execute("SELECT COUNT(*) FROM players WHERE is_guest=0 OR is_guest IS NULL").fetchone()[0]
        done_players = db.execute("SELECT COUNT(*) FROM tournament_predictions").fetchone()[0]
        rows = db.execute("""SELECT pl.name,tp.champion,tp.finalist1,tp.finalist2,tp.top_scorer,
                   tp.champion_pts,tp.finalist_pts,tp.scorer_pts,pl.id as player_id
            FROM tournament_predictions tp JOIN players pl ON tp.player_id=pl.id""").fetchall()
        result = db.execute("SELECT * FROM tournament_result WHERE id=1").fetchone()
    if started:
        return {"predictions":[dict(r) for r in rows],"result":dict(result) if result else None,
                "tournament_started":True,"first_match_time":first_mt,"done_count":done_players,"total_count":total_players}
    my_pred = next((dict(r) for r in rows if r["player_id"]==player["id"]),None)
    return {"predictions":[my_pred] if my_pred else [],"result":None,
            "tournament_started":False,"first_match_time":first_mt,"done_count":done_players,"total_count":total_players}

# ── Admin ──
class PlayerIn(BaseModel):
    name: str; telegram_chat_id: Optional[str] = None
class MatchIn(BaseModel):
    home_team: str; away_team: str; match_time: str; stage: Optional[str] = None
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
        rows = db.execute(
            "SELECT p.id, p.name, p.telegram_chat_id, p.token, p.last_seen, p.is_guest, "
            "CASE WHEN tp.player_id IS NOT NULL THEN 1 ELSE 0 END as has_tourn_pred "
            "FROM players p LEFT JOIN tournament_predictions tp ON p.id = tp.player_id"
        ).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/admin/players/{player_id}/guest", dependencies=[Depends(require_admin)])
def set_guest(player_id: int, body: dict):
    with get_db() as db:
        db.execute("UPDATE players SET is_guest=? WHERE id=?", (1 if body.get("is_guest") else 0, player_id))
    return {"ok": True}

@app.get("/api/admin/vabank-list", dependencies=[Depends(require_admin)])
def vabank_list():
    """Кто на какой матч поставил ва-банк, с прогнозом и результатом."""
    with get_db() as db:
        rows = db.execute("""
            SELECT pl.name, pl.id as player_id,
                   m.id as match_id, m.home_team, m.away_team, m.stage,
                   m.match_time, m.status, m.home_score as real_home, m.away_score as real_away,
                   p.home_score, p.away_score, p.points
            FROM predictions p
            JOIN players pl ON pl.id = p.player_id
            JOIN matches m ON m.id = p.match_id
            WHERE p.is_vabank = 1
            ORDER BY pl.name COLLATE NOCASE ASC
        """).fetchall()
    return [dict(r) for r in rows]

@app.delete("/api/admin/players/{player_id}", dependencies=[Depends(require_admin)])
def delete_player(player_id: int):
    with get_db() as db:
        # Удаляем все связанные данные
        db.execute("DELETE FROM predictions WHERE player_id=?", (player_id,))
        db.execute("DELETE FROM vabank_used WHERE player_id=?", (player_id,))
        db.execute("DELETE FROM tournament_predictions WHERE player_id=?", (player_id,))
        db.execute("DELETE FROM players WHERE id=?", (player_id,))
    return {"ok": True}

@app.post("/api/admin/matches", dependencies=[Depends(require_admin)])
def add_match(body: MatchIn):
    with get_db() as db:
        cur = db.execute("INSERT INTO matches (home_team,away_team,match_time,stage) VALUES (?,?,?,?)",
                        (body.home_team,body.away_team,body.match_time,body.stage))
    return {"id":cur.lastrowid}

@app.post("/api/admin/matches/batch", dependencies=[Depends(require_admin)])
def add_matches_batch(body: MatchBatchIn):
    added = 0
    with get_db() as db:
        for m in body.matches:
            try:
                db.execute("INSERT INTO matches (home_team,away_team,match_time,stage) VALUES (?,?,?,?)",
                          (m.home_team,m.away_team,m.match_time,m.stage))
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

class StageIn(BaseModel):
    stage: Optional[str] = None

@app.post("/api/admin/matches/{match_id}/stage", dependencies=[Depends(require_admin)])
def set_match_stage(match_id: int, body: StageIn):
    stage = (body.stage or "").strip() or None
    with get_db() as db:
        m = db.execute("SELECT id FROM matches WHERE id=?", (match_id,)).fetchone()
        if not m:
            raise HTTPException(404, "Матч не найден")
        db.execute("UPDATE matches SET stage=? WHERE id=?", (stage, match_id))
    return {"ok": True, "stage": stage}

class MatchEditIn(BaseModel):
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    stage: Optional[str] = None

@app.post("/api/admin/matches/{match_id}/edit", dependencies=[Depends(require_admin)])
def edit_match(match_id: int, body: MatchEditIn):
    """Редактирует названия команд и/или стадию. Меняет только переданные поля,
    не трогает время, счёт, статус и прогнозы."""
    with get_db() as db:
        m = db.execute("SELECT id FROM matches WHERE id=?", (match_id,)).fetchone()
        if not m:
            raise HTTPException(404, "Матч не найден")
        fields, values = [], []
        if body.home_team is not None:
            home = body.home_team.strip()
            if not home:
                raise HTTPException(400, "Название команды не может быть пустым")
            fields.append("home_team=?"); values.append(home)
        if body.away_team is not None:
            away = body.away_team.strip()
            if not away:
                raise HTTPException(400, "Название команды не может быть пустым")
            fields.append("away_team=?"); values.append(away)
        if body.stage is not None:
            fields.append("stage=?"); values.append(body.stage.strip() or None)
        if fields:
            values.append(match_id)
            db.execute(f"UPDATE matches SET {', '.join(fields)} WHERE id=?", values)
        row = db.execute("SELECT home_team, away_team, stage FROM matches WHERE id=?", (match_id,)).fetchone()
    return {"ok": True, **dict(row)}

@app.post("/api/admin/matches/{match_id}/result", dependencies=[Depends(require_admin)])
async def set_result(match_id: int, body: ResultIn):
    with get_db() as db:
        db.execute("UPDATE matches SET home_score=?,away_score=?,status='finished' WHERE id=?",
                  (body.home_score,body.away_score,match_id))
        preds = db.execute("SELECT id,home_score,away_score,is_vabank,player_id FROM predictions WHERE match_id=?", (match_id,)).fetchall()
        for p in preds:
            pts = calc_points(p["home_score"],p["away_score"],body.home_score,body.away_score,bool(p["is_vabank"]))
            db.execute("UPDATE predictions SET points=? WHERE id=?", (pts,p["id"]))
    # Рассылка результатов после матча
    async def _broadcast_safe():
        try:
            await broadcast_match_result(match_id, body.home_score, body.away_score)
        except Exception as e:
            print(f"ERROR in broadcast_match_result: {e}")
            import traceback; traceback.print_exc()
    asyncio.create_task(_broadcast_safe())
    return {"ok":True}

class LatePredictionIn(BaseModel):
    player_id: int
    home_score: int
    away_score: int
    is_vabank: bool = False
    notify: bool = True

@app.post("/api/admin/matches/{match_id}/late-prediction", dependencies=[Depends(require_admin)])
async def add_late_prediction(match_id: int, body: LatePredictionIn):
    """Ручное исключение: админ вносит прогноз за участника, который не успел
    поставить сам до начала матча (или до объявления результата). Обходит
    обычную проверку статуса матча в /api/predict, но требует явного действия
    администратора и не может быть вызвано без admin-токена."""
    if body.home_score < 0 or body.away_score < 0:
        raise HTTPException(400, "Счёт не может быть отрицательным")
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not match:
            raise HTTPException(404, "Матч не найден")
        player = db.execute("SELECT * FROM players WHERE id=?", (body.player_id,)).fetchone()
        if not player:
            raise HTTPException(404, "Участник не найден")
        existing = db.execute("SELECT id,is_vabank FROM predictions WHERE player_id=? AND match_id=?",
                              (body.player_id, match_id)).fetchone()
        already_vabank_here = bool(existing and existing["is_vabank"])
        is_vabank = 0
        if body.is_vabank:
            if not already_vabank_here:
                used = db.execute("SELECT match_id FROM vabank_used WHERE player_id=?", (body.player_id,)).fetchone()
                if used and used["match_id"] is not None and used["match_id"] != match_id:
                    raise HTTPException(400, "У этого участника ва-банк уже стоит на другом матче")
                db.execute("UPDATE predictions SET is_vabank=0 WHERE player_id=? AND match_id!=?",
                          (body.player_id, match_id))
            db.execute("INSERT OR REPLACE INTO vabank_used (player_id, match_id) VALUES (?,?)",
                      (body.player_id, match_id))
            is_vabank = 1
        elif already_vabank_here:
            db.execute("DELETE FROM vabank_used WHERE player_id=? AND match_id=?", (body.player_id, match_id))
        # Если результат матча уже известен — считаем очки сразу
        points = None
        if match["status"] == "finished" and match["home_score"] is not None and match["away_score"] is not None:
            points = calc_points(body.home_score, body.away_score, match["home_score"], match["away_score"], bool(is_vabank))
        if existing:
            db.execute("UPDATE predictions SET home_score=?,away_score=?,is_vabank=?,points=? WHERE id=?",
                      (body.home_score, body.away_score, is_vabank, points, existing["id"]))
        else:
            db.execute("INSERT INTO predictions (player_id,match_id,home_score,away_score,is_vabank,points) VALUES (?,?,?,?,?,?)",
                      (body.player_id, match_id, body.home_score, body.away_score, is_vabank, points))
    if body.notify:
        async def _broadcast_late_safe():
            try:
                await broadcast_late_correction(match_id, player["name"], body.home_score, body.away_score, bool(is_vabank), points)
            except Exception as e:
                print(f"TG error (late-prediction broadcast): {e}")
        asyncio.create_task(_broadcast_late_safe())
    return {"ok": True, "points": points}

async def broadcast_late_correction(match_id, player_name, home_score, away_score, is_vabank, points):
    """Уведомляет ВСЕХ участников о том, что администратор вручную скорректировал
    прогноз (например, у игрока не было интернета и он не успел поставить сам)."""
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        players = db.execute(
            "SELECT telegram_chat_id FROM players WHERE (is_guest=0 OR is_guest IS NULL) AND telegram_chat_id IS NOT NULL"
        ).fetchall()
    if not match:
        return
    home = team_with_flag(match["home_team"])
    away = team_with_flag(match["away_team"])
    vb_txt = " 🔥<b>ВА-БАНК</b>" if is_vabank else ""
    text = (
        f"⚠️ <b>Корректировка прогноза</b>\n"
        f"Причина: отсутствие интернета у пользователя.\n\n"
        f"⚽ <b>{home} vs {away}</b>\n"
        f"Прогноз участника {esc(player_name)}: <b>{home_score}:{away_score}</b>{vb_txt}"
    )
    if points is not None:
        text += f"\nНачислено очков: <b>{points}</b>"
    tasks = [send_telegram(str(p["telegram_chat_id"]), text) for p in players]
    if tasks:
        await asyncio.gather(*tasks)

async def broadcast_match_result(match_id: int, real_home: int, real_away: int):
    """Рассылает итоги матча всем участникам в Telegram."""
    with get_db() as db:
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        preds = db.execute("""
            SELECT pl.name, pl.telegram_chat_id, p.home_score, p.away_score, p.points, p.is_vabank
            FROM predictions p JOIN players pl ON p.player_id=pl.id
            WHERE p.match_id=? AND (pl.is_guest=0 OR pl.is_guest IS NULL) ORDER BY pl.name
        """, (match_id,)).fetchall()
        all_players = db.execute("SELECT id, name, telegram_chat_id FROM players WHERE is_guest=0 OR is_guest IS NULL ORDER BY name").fetchall()
        # Общий рейтинг после матча
        standings = db.execute("""
            SELECT pl.name,
                   COALESCE(SUM(p.points),0)
                   + COALESCE((
                       SELECT champion_pts + finalist_pts + scorer_pts
                       FROM tournament_predictions tp
                       WHERE tp.player_id = pl.id
                   ), 0) AS total
            FROM players pl
            LEFT JOIN predictions p ON pl.id=p.player_id
            WHERE pl.is_guest=0 OR pl.is_guest IS NULL
            GROUP BY pl.id ORDER BY total DESC, pl.name ASC
        """).fetchall()
    if not match:
        return

    home = team_with_flag(match['home_team'])
    away = team_with_flag(match['away_team'])
    medals = ['🥇','🥈','🥉']

    def pts_label(pts, is_vb):
        if pts >= 9: return '🎯🔥'
        if pts >= 3 and not is_vb: return '🎯'
        if pts >= 3 and is_vb: return '✅🔥'
        if pts == 1: return '✅'
        return '❌'

    # Формируем сообщение
    lines = [
        f"🏁 <b>Матч завершён!</b>",
        f"⚽ {home} <b>{real_home}:{real_away}</b> {away}\n",
        f"📊 <b>Итоги матча:</b>\n"
    ]

    # Прогнозы участников
    pred_map = {p["name"]: p for p in preds}
    for pl in all_players:
        p = pred_map.get(pl["name"])
        if p:
            vb = " 🔥" if p["is_vabank"] else ""
            label = pts_label(p["points"], bool(p["is_vabank"]))
            lines.append(f"• <b>{esc(pl['name'])}</b>{vb}: {p['home_score']}:{p['away_score']} → <b>+{p['points']} очк.</b> {label}")
        else:
            lines.append(f"• <b>{esc(pl['name'])}</b>: 😴 нет прогноза → 0 очк.")

    # Таблица лидеров после матча
    lines.append(f"\n🏆 <b>Таблица после матча:</b>\n")
    for i, r in enumerate(standings):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} {esc(r['name'])} — <b>{r['total']} очк.</b>")

    text = "\n".join(lines)
    tasks = [send_telegram(str(pl["telegram_chat_id"]), text)
             for pl in all_players if pl["telegram_chat_id"]]
    if tasks:
        await asyncio.gather(*tasks)

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

@app.get("/api/admin/tournament-result", dependencies=[Depends(require_admin)])
def get_tournament_result_admin():
    with get_db() as db:
        result = db.execute("SELECT * FROM tournament_result WHERE id=1").fetchone()
        teams_rows = db.execute("SELECT DISTINCT home_team FROM matches UNION SELECT DISTINCT away_team FROM matches ORDER BY 1").fetchall()
    teams = [r[0] for r in teams_rows]
    r = dict(result) if result else {"champion":"","finalist1":"","finalist2":"","top_scorer":""}
    return {"result": r, "teams": teams}

@app.post("/api/admin/tournament-result", dependencies=[Depends(require_admin)])
async def set_tournament_result(body: TournamentResultIn):
    """Сохраняет известные итоги турнира поэтапно и пересчитывает бонусы."""
    new_result = {
        "champion": (body.champion or "").strip(),
        "finalist1": (body.finalist1 or "").strip(),
        "finalist2": (body.finalist2 or "").strip(),
        "top_scorer": (body.top_scorer or "").strip(),  # фактически 3-е место
    }

    with get_db() as db:
        old_row = db.execute("SELECT * FROM tournament_result WHERE id=1").fetchone()
        old_result = dict(old_row) if old_row else {
            "champion": "", "finalist1": "", "finalist2": "", "top_scorer": ""
        }

        db.execute("""INSERT INTO tournament_result (id,champion,finalist1,finalist2,top_scorer)
            VALUES (1,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET champion=excluded.champion,finalist1=excluded.finalist1,
              finalist2=excluded.finalist2,top_scorer=excluded.top_scorer""",
            (new_result["champion"], new_result["finalist1"], new_result["finalist2"], new_result["top_scorer"]))

        preds = db.execute("SELECT * FROM tournament_predictions").fetchall()
        finalist_results = {
            value.lower() for value in (new_result["finalist1"], new_result["finalist2"]) if value
        }
        for tp in preds:
            champion_pts = 10 if (
                new_result["champion"] and tp["champion"] and
                tp["champion"].strip().lower() == new_result["champion"].lower()
            ) else 0

            finalist_pts = 0
            # Каждый из двух прогнозов финалиста оценивается отдельно. Пустые итоги не совпадают.
            for predicted_finalist in (tp["finalist1"], tp["finalist2"]):
                if predicted_finalist and predicted_finalist.strip().lower() in finalist_results:
                    finalist_pts += 5

            third_pts = 5 if (
                new_result["top_scorer"] and tp["top_scorer"] and
                tp["top_scorer"].strip().lower() == new_result["top_scorer"].lower()
            ) else 0

            db.execute(
                "UPDATE tournament_predictions SET champion_pts=?,finalist_pts=?,scorer_pts=? WHERE player_id=?",
                (champion_pts, finalist_pts, third_pts, tp["player_id"]),
            )

    changed = {
        key: {"old": (old_result.get(key) or "").strip(), "new": value}
        for key, value in new_result.items()
        if (old_result.get(key) or "").strip() != value
    }

    # Пустое повторное сохранение не создаёт лишнюю рассылку.
    if changed:
        async def _broadcast_safe():
            try:
                await broadcast_tournament_result_changes(changed, new_result)
            except Exception as e:
                print(f"ERROR in broadcast_tournament_result_changes: {e}")
                import traceback; traceback.print_exc()
        asyncio.create_task(_broadcast_safe())

    return {"ok": True, "changed": list(changed.keys())}


async def broadcast_tournament_result_changes(changed, current_result):
    """Рассылает только новые/изменённые итоги турнира и актуальную таблицу."""
    with get_db() as db:
        players = db.execute("""
            SELECT pl.name, pl.telegram_chat_id,
                   tp.champion_pts, tp.finalist_pts, tp.scorer_pts
            FROM players pl
            LEFT JOIN tournament_predictions tp ON pl.id=tp.player_id
            WHERE pl.is_guest=0 OR pl.is_guest IS NULL
            ORDER BY pl.name
        """).fetchall()
        standings = db.execute("""
            SELECT pl.name,
                   COALESCE(SUM(p.points),0)+COALESCE((SELECT champion_pts+finalist_pts+scorer_pts FROM tournament_predictions tp WHERE tp.player_id=pl.id),0) as total
            FROM players pl
            LEFT JOIN predictions p ON pl.id=p.player_id
            WHERE pl.is_guest=0 OR pl.is_guest IS NULL
            GROUP BY pl.id ORDER BY total DESC, pl.name ASC
        """).fetchall()

    labels = {
        "champion": ("🏆", "Чемпион", 10),
        "finalist1": ("🥈", "Первый финалист", 5),
        "finalist2": ("🥈", "Второй финалист", 5),
        "top_scorer": ("🥉", "3-е место", 5),
    }

    nonempty_new = [(key, data) for key, data in changed.items() if data["new"]]
    cleared = [(key, data) for key, data in changed.items() if not data["new"]]
    corrected = [(key, data) for key, data in nonempty_new if data["old"]]
    newly_set = [(key, data) for key, data in nonempty_new if not data["old"]]

    # Заголовок зависит от того, что именно определилось.
    if len(newly_set) == 1 and not corrected and not cleared:
        key, data = newly_set[0]
        if key == "finalist1":
            title = "🥈 <b>Определился первый финалист ЧМ-2026!</b>"
        elif key == "finalist2":
            title = "🥈 <b>Определился второй финалист ЧМ-2026!</b>"
        elif key == "champion":
            title = "🏆 <b>Определился чемпион ЧМ-2026!</b>"
        else:
            title = "🥉 <b>Определилась команда, занявшая 3-е место!</b>"
    elif corrected:
        title = "✏️ <b>Итоги турнира обновлены</b>"
    else:
        title = "🏆 <b>Новые итоги турнира!</b>"

    lines = [title, ""]

    for key, data in nonempty_new:
        icon, label, pts = labels[key]
        team = team_with_flag(data["new"])
        if data["old"]:
            old_team = team_with_flag(data["old"])
            lines.append(f"{icon} {label}: <s>{old_team}</s> → <b>{team}</b>")
        else:
            if key in ("finalist1", "finalist2"):
                lines.append(f"{icon} <b>{team}</b> выходит в финал")
            elif key == "champion":
                lines.append(f"{icon} Чемпион мира: <b>{team}</b>")
            else:
                lines.append(f"{icon} 3-е место: <b>{team}</b>")
        lines.append(f"🎯 За правильный прогноз: <b>+{pts} очков</b>")
        lines.append("")

    for key, data in cleared:
        icon, label, _ = labels[key]
        old_team = team_with_flag(data["old"]) if data["old"] else "—"
        lines.append(f"{icon} {label} отменён: <s>{old_team}</s>")
        lines.append("Бонусы пересчитаны.")
        lines.append("")

    # Показываем бонусы, полученные именно за категории, изменённые в этом сохранении.
    changed_categories = set(changed.keys())
    lines.append("📊 <b>Бонусы участников после обновления:</b>")
    lines.append("")
    any_bonus = False
    for pl in players:
        delta_parts = []
        if "champion" in changed_categories and pl["champion_pts"]:
            delta_parts.append(f"чемпион +{pl['champion_pts']}")
        if ({"finalist1", "finalist2"} & changed_categories) and pl["finalist_pts"]:
            delta_parts.append(f"финалисты +{pl['finalist_pts']}")
        if "top_scorer" in changed_categories and pl["scorer_pts"]:
            delta_parts.append(f"3-е место +{pl['scorer_pts']}")
        if delta_parts:
            any_bonus = True
            lines.append(f"• {esc(pl['name'])} — <b>{', '.join(delta_parts)}</b>")

    if not any_bonus:
        lines.append("Никто не угадал этот результат.")

    lines.append("")
    lines.append("🏆 <b>Текущая таблица лидеров:</b>")
    lines.append("")
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(standings):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} {esc(r['name'])} — <b>{r['total']} очк.</b>")

    pending = []
    if not current_result.get("finalist1") or not current_result.get("finalist2"):
        pending.append("финалисты")
    if not current_result.get("champion"):
        pending.append("чемпион")
    if not current_result.get("top_scorer"):
        pending.append("3-е место")
    if pending:
        lines.append("")
        lines.append(f"⏳ Ещё не определены: {', '.join(pending)}.")

    text = "\n".join(lines)
    tasks = [
        send_telegram(str(pl["telegram_chat_id"]), text)
        for pl in players if pl["telegram_chat_id"]
    ]
    if tasks:
        await asyncio.gather(*tasks)
    print("Tournament result update broadcast sent!")

@app.get("/api/tournament-settings")
def get_tournament_settings(token: str):
    get_player_by_token(token)
    with get_db() as db:
        row = db.execute("SELECT * FROM tournament_settings WHERE id=1").fetchone()
        player_count = db.execute("SELECT COUNT(*) FROM players WHERE is_guest=0 OR is_guest IS NULL").fetchone()[0]
    result = dict(row) if row else {"entry_fee": 15000, "prize_config": "60,30,10", "hide_days": 0}
    result["player_count"] = player_count
    return result

class TournamentSettingsIn(BaseModel):
    entry_fee: int
    prize_config: str
    hide_days: int = 0

@app.post("/api/admin/tournament-settings", dependencies=[Depends(require_admin)])
def save_tournament_settings(body: TournamentSettingsIn):
    with get_db() as db:
        db.execute("""INSERT INTO tournament_settings (id, entry_fee, prize_config, hide_days)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET entry_fee=excluded.entry_fee, prize_config=excluded.prize_config, hide_days=excluded.hide_days""",
            (body.entry_fee, body.prize_config, body.hide_days))
    return {"ok": True}

@app.post("/api/admin/hide-days", dependencies=[Depends(require_admin)])
def set_hide_days(body: dict):
    days = int(body.get("hide_days", 0))
    with get_db() as db:
        db.execute("UPDATE tournament_settings SET hide_days=? WHERE id=1", (days,))
    return {"ok": True}

@app.post("/api/telegram/webhook")
async def telegram_webhook(update: dict, x_telegram_bot_api_secret_token: Optional[str] = Header(None)):
    # Проверка секрета — только если он задан (обратная совместимость)
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")
    msg = update.get("message",{})
    text = msg.get("text","")
    chat_id = str(msg.get("chat",{}).get("id",""))
    if text.startswith("/start "):
        token = text.split(" ",1)[1].strip()
        with get_db() as db:
            player = db.execute("SELECT * FROM players WHERE token=?", (token,)).fetchone()
            if player:
                db.execute("UPDATE players SET telegram_chat_id=? WHERE token=?", (chat_id,token))
                await send_telegram(chat_id, f"✅ Привет, <b>{esc(player['name'])}</b>! Ты подключён к турниру прогнозов ⚽\nУдачи! 🏆")
            else:
                await send_telegram(chat_id, "❌ Неверный токен.")
    return {"ok":True}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
