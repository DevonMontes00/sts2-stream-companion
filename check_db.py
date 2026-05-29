import database

database.init_db()
conn = database.get_connection()

runs = conn.execute("SELECT run_id, ascension, floor, victory, is_multiplayer, killed_by_encounter FROM runs").fetchall()
print(f"Runs stored: {len(runs)}")
for r in runs:
    print(dict(r))

players = conn.execute("SELECT run_id, player_index, character, final_gold, final_hp FROM run_players").fetchall()
print(f"\nPlayers stored: {len(players)}")
for p in players:
    print(dict(p))

cards  = conn.execute("SELECT COUNT(*) as total FROM run_cards").fetchone()
relics = conn.execute("SELECT COUNT(*) as total FROM run_relics").fetchone()
print(f"\nCards stored:  {cards['total']}")
print(f"Relics stored: {relics['total']}")

# Killer breakdown - what's ending runs most
killers = conn.execute("""
    SELECT killed_by_encounter, COUNT(*) as count
    FROM runs
    WHERE killed_by_encounter != 'NONE.NONE'
    GROUP BY killed_by_encounter
    ORDER BY count DESC
    LIMIT 10
""").fetchall()
print("\n--- Top killers ---")
for k in killers:
    print(dict(k))

# Winrate by character
char_wr = conn.execute("""
    SELECT rp.character,
           COUNT(DISTINCT r.run_id) as runs,
           SUM(r.victory) as wins,
           ROUND(100.0 * SUM(r.victory) / COUNT(*), 1) as winrate
    FROM run_players rp
    JOIN runs r ON r.run_id = rp.run_id
    GROUP BY rp.character
    ORDER BY runs DESC
""").fetchall()
print("\n--- Winrate by character ---")
for c in char_wr:
    print(dict(c))