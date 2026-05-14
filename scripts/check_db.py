import sys, sqlite3, pandas as pd
sys.path.insert(0, 'scripts')
from config import DB_PATH

conn = sqlite3.connect(DB_PATH)
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("Tables:", tables)

if "meta_ads_diario" in tables:
    df = pd.read_sql("SELECT producto, SUM(gasto_eur) as gasto, SUM(compras_meta) as compras FROM meta_ads_diario GROUP BY producto ORDER BY gasto DESC", conn)
    print("\nMeta por producto:")
    print(df.to_string(index=False))
elif "meta_ads" in tables:
    df = pd.read_sql("SELECT producto, SUM(gasto_eur) as gasto, SUM(compras) as compras FROM meta_ads GROUP BY producto ORDER BY gasto DESC", conn)
    print("\nMeta por producto:")
    print(df.to_string(index=False))
elif "meta_campanas" in tables:
    df = pd.read_sql("SELECT producto, SUM(gasto_eur) as gasto, SUM(compras) as compras FROM meta_campanas GROUP BY producto ORDER BY gasto DESC", conn)
    print("\nMeta por producto (meta_campanas):")
    print(df.to_string(index=False))
else:
    # Try first meta table
    meta_tables = [t for t in tables if "meta" in t.lower()]
    print("Meta tables:", meta_tables)
    if meta_tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({meta_tables[0]})").fetchall()]
        print(f"Columns in {meta_tables[0]}:", cols)

conn.close()
