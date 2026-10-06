import sqlite3
import libsql_experimental as libsql

# 1. Wklej poniżej swoje dane z Turso (te same, które podawałeś w Secrets na Renderze)
TURSO_URL = "libsql://dieta-dargenx111.aws-eu-west-1.turso.io"
TURSO_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3OTExOTU0NjksImlkIjoiMDFhMTBiOTEtOWYwMS03NTRkLTgwNTctODMxNDAwNWZlYzc4Iiwia2lkIjoid2l6QVFKNXY4d2lXdFJGU2hOVUxWTjJzUHAtVmxhYTRkWF9Ecjl3emJfdyIsInJpZCI6Ijk3Zjg1Mjk3LTY1MjEtNDQyMy1iYzMzLTUwZjY5NGFkYjFjYiJ9.XEyc0NsajKi2EYXPwrHHYqswugvNGCQLYHox-mbMIMNgyetq4USJbS1Cz7t6AdSVPnKlbzN75j08DWepVisTCg"

print("🔌 Łączenie z lokalną bazą SQLite oraz chmurą Turso...")
local_conn = sqlite3.connect("przepisy.db")
turso_conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)

# Pobranie list tabel z lokalnego pliku przepisy.db
tables = [r[0] for r in local_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]

for table in tables:
    print(f"📦 Przesyłanie tabeli: {table}...")
    
    # Pobranie kolumn
    columns_info = local_conn.execute(f"PRAGMA table_info({table})").fetchall()
    cols = [c[1] for c in columns_info]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    
    # Pobranie wierszy
    rows = local_conn.execute(f"SELECT * FROM {table}").fetchall()
    
    # Czyszczenie docelowej tabeli w Turso przed wgraniem
    try:
        turso_conn.execute(f"DELETE FROM {table}")
    except Exception:
        pass
        
    # Wgrywanie wierszy do chmury
    for row in rows:
        turso_conn.execute(f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})", row)

turso_conn.commit()
print("\n✅ SUKCES! Cała zawartość pliku przepisy.db została wgrana do chmury Turso.")