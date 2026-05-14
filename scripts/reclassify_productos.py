"""
Reclassifies the 'producto' column in meta_ads_diario based on the current PRODUCTO_MAP.
Run this after updating PRODUCTO_MAP in config.py.
"""
import sys, sqlite3
sys.path.insert(0, 'scripts')
from config import DB_PATH, producto_desde_campana

conn = sqlite3.connect(DB_PATH)

rows = conn.execute("SELECT id, campana FROM meta_ads_diario").fetchall()
print(f"Rows to reclassify: {len(rows)}")

updates = [(producto_desde_campana(campana), row_id) for row_id, campana in rows]
conn.executemany("UPDATE meta_ads_diario SET producto = ? WHERE id = ?", updates)
conn.commit()

# Summary
summary = conn.execute(
    "SELECT producto, COUNT(*) as filas, SUM(gasto_eur) as gasto, SUM(compras_meta) as compras "
    "FROM meta_ads_diario GROUP BY producto ORDER BY gasto DESC"
).fetchall()

print("\nProducto breakdown post-reclassificación:")
print(f"{'Producto':<35} {'Filas':>6} {'Gasto EUR':>12} {'Compras':>9}")
print("-" * 65)
for prod, filas, gasto, compras in summary:
    print(f"{prod:<35} {filas:>6} {gasto or 0:>12.2f} {compras or 0:>9.0f}")

conn.close()
print("\n✅ Reclassificación completa.")
