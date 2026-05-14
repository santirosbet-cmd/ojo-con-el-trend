"""db_setup.py — Schema SQLite completo + migraciones no destructivas."""

import sqlite3
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from config import DB_PATH


def create_db(db_path=None):
    path = db_path or DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS semanas (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha_reporte    DATE NOT NULL,
        periodo_inicio   DATE NOT NULL,
        periodo_fin      DATE NOT NULL,
        created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(fecha_reporte)
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS ordenes_dropi (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        semana_id         INTEGER REFERENCES semanas(id),
        orden_id          TEXT,
        fecha             DATE,
        cliente           TEXT,
        tienda            TEXT,
        estatus           TEXT,
        valor_venta       REAL DEFAULT 0,
        costo_producto    REAL DEFAULT 0,
        flete             REAL DEFAULT 0,
        costo_devolucion  REAL DEFAULT 0,
        ganancia_dropi    REAL DEFAULT 0,
        transportadora    TEXT,
        novedad           TEXT,
        es_prueba         INTEGER DEFAULT 0,
        ciudad_destino    TEXT,
        departamento      TEXT,
        categorias        TEXT,
        tipo_envio        TEXT,
        numero_guia       TEXT,
        valor_facturado   REAL DEFAULT 0,
        ingestion_date    DATE DEFAULT CURRENT_DATE
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS meta_ads_diario (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        semana_id       INTEGER REFERENCES semanas(id),
        fecha           DATE,
        campana         TEXT,
        adset           TEXT,
        anuncio         TEXT,
        producto        TEXT,
        gasto_eur       REAL DEFAULT 0,
        compras_meta    REAL DEFAULT 0,
        valor_conv_eur  REAL DEFAULT 0,
        roas_meta       REAL DEFAULT 0,
        cpa_meta        REAL DEFAULT 0,
        clics           INTEGER DEFAULT 0,
        impresiones     INTEGER DEFAULT 0,
        alcance         INTEGER DEFAULT 0,
        frecuencia      REAL DEFAULT 0,
        cpm             REAL DEFAULT 0,
        ctr             REAL DEFAULT 0,
        cpc             REAL DEFAULT 0,
        carritos        INTEGER DEFAULT 0,
        video_25pct     INTEGER DEFAULT 0,
        video_50pct     INTEGER DEFAULT 0,
        avg_video_play  REAL DEFAULT 0,
        ingestion_date  DATE DEFAULT CURRENT_DATE
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS metricas_semanales (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        semana_id            INTEGER REFERENCES semanas(id) UNIQUE,
        ventas_confirmadas   REAL DEFAULT 0,
        ventas_pipeline      REAL DEFAULT 0,
        cogs                 REAL DEFAULT 0,
        flete_total          REAL DEFAULT 0,
        costo_devoluciones   REAL DEFAULT 0,
        gasto_ads_eur        REAL DEFAULT 0,
        gasto_ads_mxn        REAL DEFAULT 0,
        tasa_eur_mxn         REAL DEFAULT 21.5,
        margen_bruto         REAL DEFAULT 0,
        margen_bruto_pct     REAL DEFAULT 0,
        utilidad_neta        REAL DEFAULT 0,
        utilidad_neta_pct    REAL DEFAULT 0,
        mer                  REAL DEFAULT 0,
        aov                  REAL DEFAULT 0,
        cpa_real             REAL DEFAULT 0,
        cpa_breakeven        REAL DEFAULT 0,
        roas_real            REAL DEFAULT 0,
        roas_meta_promedio   REAL DEFAULT 0,
        total_ordenes        INTEGER DEFAULT 0,
        ordenes_prueba       INTEGER DEFAULT 0,
        entregadas           INTEGER DEFAULT 0,
        canceladas_reales    INTEGER DEFAULT 0,
        en_camino            INTEGER DEFAULT 0,
        devoluciones         INTEGER DEFAULT 0,
        tasa_cancelacion     REAL DEFAULT 0,
        pct_entrega          REAL DEFAULT 0,
        compras_meta_total   INTEGER DEFAULT 0,
        clics_total          INTEGER DEFAULT 0,
        ctr_promedio         REAL DEFAULT 0,
        cpc_promedio         REAL DEFAULT 0,
        caja_neta            REAL DEFAULT 0,
        dias_pauta_restantes REAL DEFAULT 0,
        gasto_diario_ads     REAL DEFAULT 0
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS transportadoras_semanales (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        semana_id      INTEGER REFERENCES semanas(id),
        transportadora TEXT,
        total          INTEGER DEFAULT 0,
        entregadas     INTEGER DEFAULT 0,
        canceladas     INTEGER DEFAULT 0,
        novedades      INTEGER DEFAULT 0,
        pct_entrega    REAL DEFAULT 0,
        costo_hundido  REAL DEFAULT 0
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS cartera_dropi (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        transaccion_id  TEXT UNIQUE,
        fecha           TEXT,
        tipo            TEXT,
        subtipo         TEXT,
        monto           REAL DEFAULT 0,
        monto_previo    REAL DEFAULT 0,
        orden_id        TEXT,
        numero_guia     TEXT,
        descripcion     TEXT,
        concepto_retiro TEXT,
        ingestion_date  DATE DEFAULT CURRENT_DATE
    )""")

    conn.commit()

    # Migraciones no destructivas — añade columnas nuevas a tablas existentes
    _migrate(conn)

    conn.close()
    print(f"✅ Base de datos lista: {path}")


def _migrate(conn):
    """Añade columnas que no existen aún (idempotente)."""
    c = conn.cursor()
    migrations = {
        "ordenes_dropi": [
            "ciudad_destino TEXT",
            "departamento TEXT",
            "categorias TEXT",
            "tipo_envio TEXT",
            "numero_guia TEXT",
            "valor_facturado REAL DEFAULT 0",
        ],
        "meta_ads_diario": [
            "adset TEXT",
            "producto TEXT",
            "alcance INTEGER DEFAULT 0",
            "frecuencia REAL DEFAULT 0",
            "cpm REAL DEFAULT 0",
            "video_25pct INTEGER DEFAULT 0",
            "video_50pct INTEGER DEFAULT 0",
            "avg_video_play REAL DEFAULT 0",
        ],
    }
    for table, cols in migrations.items():
        for col_def in cols:
            col_name = col_def.split()[0]
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            except Exception:
                pass  # columna ya existe
    conn.commit()


if __name__ == "__main__":
    create_db()
