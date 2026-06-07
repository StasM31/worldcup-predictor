"""Скрипт для создания тестовых данных. Запустить ОДИН раз вручную через Railway Console."""
import sqlite3, secrets, random, os

_DATA_DIR = "/app/data" if os.path.isdir("/app/data") else "."
DATABASE = os.path.join(_DATA_DIR, "predictor.db")

conn = sqlite3.connect(DATABASE)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Add test players
test_players = ["Андрюха", "Ванька", "Славик", "Василий", "Олег"]
tokens = {}
for name in test_players:
    token = secrets.token_urlsafe(16)
    try:
        cur.execute("INSERT INTO players (name, token) VALUES (?, ?)", (name, token))
        tokens[name] = token
        print(f"Added: {name}")
    except Exception as e:
        print(f"Skip {name}: {e}")
conn.commit()

# Get all player ids
players = dict(cur.execute("SELECT name, id FROM players").fetchall())

# Add 15 finished matches
matches_data = [
    ("Мексика", "ЮАР", "2026-06-11T19:00", 2, 1),
    ("Южная Корея", "Чехия", "2026-06-12T02:00", 0, 0),
    ("Канада", "Босния и Герцеговина", "2026-06-12T19:00", 3, 1),
    ("США", "Парагвай", "2026-06-13T01:00", 2, 0),
    ("Бразилия", "Марокко", "2026-06-13T22:00", 3, 0),
    ("Германия", "Кюрасао", "2026-06-14T17:00", 4, 0),
    ("Австралия", "Турция", "2026-06-14T04:00", 1, 2),
    ("Нидерланды", "Япония", "2026-06-14T20:00", 2, 1),
    ("Испания", "Иордания", "2026-06-15T22:00", 3, 0),
    ("Аргентина", "Перу", "2026-06-15T01:00", 2, 2),
    ("Франция", "Узбекистан", "2026-06-16T02:00", 4, 1),
    ("Португалия", "Ирак", "2026-06-16T20:00", 2, 0),
    ("Англия", "Сербия", "2026-06-17T02:00", 1, 0),
    ("Италия", "Эквадор", "2026-06-17T19:00", 1, 1),
    ("Хорватия", "Марокко", "2026-06-18T02:00", 0, 1),
]

match_ids = []
for home, away, time, hs, as_ in matches_data:
    cur.execute("""INSERT INTO matches (home_team, away_team, match_time, home_score, away_score, status)
        VALUES (?, ?, ?, ?, ?, 'finished')""", (home, away, time, hs, as_))
    match_ids.append(cur.lastrowid)
conn.commit()
print(f"Added {len(match_ids)} matches")

def calc_points(ph, pa, rh, ra):
    if ph==rh and pa==ra: return 3
    po = "H" if ph>pa else ("A" if ph<pa else "D")
    ro = "H" if rh>ra else ("A" if rh<ra else "D")
    return 1 if po==ro else 0

# Add random predictions for all players on all matches
all_players = cur.execute("SELECT id, name FROM players").fetchall()
for match_id, (home, away, time, hs, as_) in zip(match_ids, matches_data):
    for pl in all_players:
        ph = random.randint(0, 4)
        pa = random.randint(0, 4)
        pts = calc_points(ph, pa, hs, as_)
        try:
            cur.execute("""INSERT INTO predictions (player_id, match_id, home_score, away_score, points)
                VALUES (?, ?, ?, ?, ?)""", (pl[0], match_id, ph, pa, pts))
        except: pass

conn.commit()
print("Predictions added!")
conn.close()
print("Done!")
