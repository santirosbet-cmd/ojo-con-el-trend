"""
ingest.py — Ingesta Dropi (Excel) + Meta Ads (CSV) → SQLite
Modos:
  - Por semana (upload desde dashboard)
  - Bulk histórico desde carpetas raw/
"""

import pandas as pd
import numpy as np
import sqlite3
import argparse
import unicodedata
import warnings
import sys
import glob
from datetime import date, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    TASA_EUR_MXN, TEST_APELLIDO_EXCLUIDO, TEST_NOMBRES_COMPLETOS,
    DB_PATH, DEV_ESTADOS, PIPELINE_EXCLUIDOS, DROPI_COLS, META_COLS,
    RAW_DROPI_DIR, RAW_META_DIR, RAW_CARTERA_DIR, producto_desde_campana,
)

warnings.filterwarnings("ignore")


# ── HELPERS ───────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(c) != "Mn"
    )

_NOMBRES_NORM = [_norm(t) for t in TEST_NOMBRES_COMPLETOS]

def is_test_order(name: str) -> bool:
    if pd.isna(name):
        return False
    n = _norm(str(name).strip())
    if TEST_APELLIDO_EXCLUIDO in n:
        return True
    return n in _NOMBRES_NORM

def _find_col(df: pd.DataFrame, *candidates) -> str | None:
    """Devuelve el primer nombre de columna que coincide con algún candidato (accent-insensitive)."""
    col_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        key = _norm(str(cand))
        if key in col_map:
            return col_map[key]
        # partial match
        for k, v in col_map.items():
            if key in k:
                return v
    return None

def _safe_float(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series([default] * len(df), index=df.index)
    return pd.to_numeric(df[col], errors="coerce").fillna(default)

def _safe_int(df: pd.DataFrame, col: str) -> pd.Series:
    return _safe_float(df, col).astype(int)

def _safe_str(df: pd.DataFrame, col: str, default: str = "") -> pd.Series:
    if col not in df.columns:
        return pd.Series([default] * len(df), index=df.index)
    return df[col].fillna(default).astype(str).str.strip()


# ── LOADERS ───────────────────────────────────────────────────────────────

def load_dropi(filepath: str) -> pd.DataFrame:
    df = pd.read_excel(filepath, dtype=str)
    # Normalizar nombres de columna (quitar encoding roto y espacios)
    df.columns = [_norm(c).upper().strip() for c in df.columns]

    numeric_cols = [
        "VALOR DE COMPRA EN PRODUCTOS",
        "TOTAL EN PRECIOS DE PROVEEDOR",
        "PRECIO FLETE",
        "COSTO DEVOLUCION FLETE",
        "GANANCIA",
        "VALOR FACTURADO",
    ]
    # Los nombres ya están normalizados — buscar con _find_col sobre el df normalizado
    for col in numeric_cols:
        norm_col = _norm(col).upper()
        if norm_col in df.columns:
            df[norm_col] = pd.to_numeric(df[norm_col], errors="coerce").fillna(0)
        else:
            df[norm_col] = 0.0

    # Columna estatus
    est_col = _find_col(df, "ESTATUS") or "ESTATUS"
    df["ESTATUS"] = df.get(est_col, pd.Series(dtype=str)).fillna("").str.upper().str.strip()

    # Flag prueba
    cli_col = _find_col(df, "NOMBRE CLIENTE") or "NOMBRE CLIENTE"
    df["es_prueba"] = df.get(cli_col, pd.Series(dtype=str)).apply(is_test_order).astype(int)

    return df


def load_meta(filepath: str) -> pd.DataFrame:
    meta = pd.read_csv(filepath, dtype=str)
    meta.columns = [c.strip() for c in meta.columns]

    # Renombrar columnas con sufijo EUR extra y variantes de nombre
    # Meta puede exportar "Resultados / ROAS de resultados" en lugar de "Compras / ROAS de compras"
    RENAME = {
        # Variantes EUR
        "CPC (costo por clic en el enlace) (EUR)":         "CPC (costo por clic en el enlace)",
        "Costo por compra (EUR)":                          "Costo por compra",
        "CPM (costo por mil impresiones) (EUR)":           "CPM (costo por mil impresiones)",
        "Costo por artículo agregado al carrito (EUR)":    "Costo por artículo agregado al carrito",
        "Costo por visita a la página de destino (EUR)":   "Costo por visita a la página de destino",
        # Variantes "Resultados" (cuando el informe no usa columna dedicada de Compras)
        "Resultados":                                      "Compras",
        "ROAS de resultados":                              "ROAS de compras",
        "Costo por resultado":                             "Costo por compra",
        "Valor de resultados":                             "Valor de conversión de compras",
    }
    meta = meta.rename(columns={k: v for k, v in RENAME.items() if k in meta.columns})

    # Cuando el informe usa "Tipo de resultado" (breakdown por tipo de conversión):
    # – Eliminar la fila resumen "mixed" (es un total que duplica el gasto)
    # – Para filas que NO son compras, poner Compras=0 y ROAS=0 (no son conversiones de compra)
    # – Deduplicar dia+campaña+anuncio dando prioridad a filas de "Compras en el sitio web"
    tipo_col = next((c for c in meta.columns if _norm(c) == "tipo de resultado"), None)
    if tipo_col:
        meta = meta[meta[tipo_col].fillna("") != "mixed"].copy()
        es_compra = meta[tipo_col].fillna("").str.lower().str.contains("compra")
        meta.loc[~es_compra, "Compras"] = "0"
        meta.loc[~es_compra, "ROAS de compras"] = "0"
        meta.loc[~es_compra, "Costo por compra"] = "0"
        meta.loc[~es_compra, "Valor de conversión de compras"] = "0"
        # Ordenar para que filas de compras queden primero antes del dedup
        meta["_es_compra_sort"] = (~es_compra).astype(int)
        meta = meta.sort_values("_es_compra_sort").drop(columns="_es_compra_sort")

    # Columna fecha
    if "Día" not in meta.columns:
        for alt in ["Inicio del informe", "Fecha", "Date", "D\xeda"]:
            if alt in meta.columns:
                meta["Día"] = meta[alt]
                break
    if "Día" not in meta.columns:
        # Try accent-insensitive
        for c in meta.columns:
            if _norm(c) == "dia":
                meta["Día"] = meta[c]
                break

    # Columnas opcionales
    for col in ("Nombre del anuncio", "Valor de conversión de compras",
                "Nombre del conjunto de anuncios", "Alcance", "Frecuencia",
                "CPM (costo por mil impresiones)",
                "Reproducciones de video hasta el 25%",
                "Reproducciones de video hasta el 50%",
                "Tiempo promedio de reproducción del video",
                "Clics (todos)", "CTR (todos)"):
        if col not in meta.columns:
            meta[col] = 0

    numeric_cols = [
        "Importe gastado (EUR)", "Compras", "ROAS de compras", "Costo por compra",
        "Clics en el enlace", "Impresiones", "Alcance", "Frecuencia",
        "CTR (porcentaje de clics en el enlace)", "CPC (costo por clic en el enlace)",
        "Artículos agregados al carrito", "Valor de conversión de compras",
        "CPM (costo por mil impresiones)",
        "Reproducciones de video hasta el 25%", "Reproducciones de video hasta el 50%",
        "Tiempo promedio de reproducción del video",
    ]
    for col in numeric_cols:
        if col in meta.columns:
            meta[col] = pd.to_numeric(
                meta[col].astype(str).str.replace(",", "").str.strip(), errors="coerce"
            ).fillna(0)
        else:
            meta[col] = 0.0

    meta = meta[meta["Importe gastado (EUR)"] > 0].copy()

    # Producto desde nombre de campaña
    camp_col = "Nombre de la campaña"
    if camp_col not in meta.columns:
        for c in meta.columns:
            if _norm(c) == _norm(camp_col):
                camp_col = c
                break
    meta["producto"] = meta[camp_col].apply(producto_desde_campana)

    return meta


# ── MÉTRICAS ──────────────────────────────────────────────────────────────

def calculate_metrics(df: pd.DataFrame, meta: pd.DataFrame, tasa: float) -> dict:
    # Normalizar columnas de df (ya deberían estar normalizadas)
    def gcol(key):
        norm_key = _norm(key).upper()
        if norm_key in df.columns:
            return norm_key
        return key

    VENTA   = gcol("VALOR DE COMPRA EN PRODUCTOS")
    COSTO   = gcol("TOTAL EN PRECIOS DE PROVEEDOR")
    FLETE   = gcol("PRECIO FLETE")
    DEVFLT  = gcol("COSTO DEVOLUCION FLETE")

    df_real = df[df["es_prueba"] == 0].copy()

    entregadas   = df_real[df_real["ESTATUS"] == "ENTREGADO"]
    canceladas   = df_real[df_real["ESTATUS"] == "CANCELADO"]
    devoluciones = df_real[df_real["ESTATUS"].isin(DEV_ESTADOS)]
    en_camino    = df_real[~df_real["ESTATUS"].isin(PIPELINE_EXCLUIDOS)]

    ventas_conf  = float(entregadas[VENTA].sum())
    cogs         = float(entregadas[COSTO].sum())
    flete        = float(entregadas[FLETE].sum())
    margen_bruto = ventas_conf - cogs - flete
    ventas_camp  = float(en_camino[VENTA].sum())
    costo_devol  = float(devoluciones[DEVFLT].sum() + devoluciones[FLETE].sum())

    gasto_eur = float(meta["Importe gastado (EUR)"].sum()) if not meta.empty else 0.0
    gasto_mxn = gasto_eur * tasa
    utilidad  = margen_bruto - gasto_mxn

    n_ent = max(len(entregadas), 1)
    aov_series = df_real[df_real["ESTATUS"] != "CANCELADO"][VENTA]
    aov = float(aov_series.mean()) if len(aov_series) > 0 else 0.0

    cpa_real      = gasto_mxn / n_ent
    cpa_breakeven = aov - (cogs / n_ent) - (flete / n_ent)
    roas_real     = ventas_conf / max(gasto_mxn, 1)
    mer           = ventas_conf / max(gasto_mxn, 1)

    compras_meta = int(meta["Compras"].sum()) if not meta.empty else 0
    clics        = int(meta["Clics en el enlace"].sum()) if not meta.empty else 0
    ctr_col, cpc_col = "CTR (porcentaje de clics en el enlace)", "CPC (costo por clic en el enlace)"
    ctr_prom = float(meta[ctr_col].mean()) if (not meta.empty and ctr_col in meta.columns) else 0.0
    cpc_prom = float(meta[cpc_col].mean()) if (not meta.empty and cpc_col in meta.columns) else 0.0

    roas_meta_vals = meta["ROAS de compras"].replace(0, np.nan) if not meta.empty else pd.Series()
    roas_meta = float(roas_meta_vals.mean()) if roas_meta_vals.notna().any() else 0.0

    n_dias = max(meta["Día"].nunique() if (not meta.empty and "Día" in meta.columns) else 1, 1)
    gasto_diario = gasto_mxn / n_dias
    caja_neta    = ventas_conf - gasto_mxn
    dias_pauta   = caja_neta / max(gasto_diario, 1)

    total_ord = len(df_real)
    n_pruebas = int(df["es_prueba"].sum())

    return {
        "ventas_confirmadas":   round(ventas_conf, 2),
        "ventas_pipeline":      round(ventas_camp, 2),
        "cogs":                 round(cogs, 2),
        "flete_total":          round(flete, 2),
        "costo_devoluciones":   round(costo_devol, 2),
        "gasto_ads_eur":        round(gasto_eur, 2),
        "gasto_ads_mxn":        round(gasto_mxn, 2),
        "tasa_eur_mxn":         tasa,
        "margen_bruto":         round(margen_bruto, 2),
        "margen_bruto_pct":     round(margen_bruto / max(ventas_conf, 1) * 100, 1),
        "utilidad_neta":        round(utilidad, 2),
        "utilidad_neta_pct":    round(utilidad / max(ventas_conf, 1) * 100, 1),
        "mer":                  round(mer, 3),
        "aov":                  round(aov, 2),
        "cpa_real":             round(cpa_real, 2),
        "cpa_breakeven":        round(cpa_breakeven, 2),
        "roas_real":            round(roas_real, 3),
        "roas_meta_promedio":   round(roas_meta, 2),
        "total_ordenes":        total_ord,
        "ordenes_prueba":       n_pruebas,
        "entregadas":           len(entregadas),
        "canceladas_reales":    len(canceladas),
        "en_camino":            len(en_camino),
        "devoluciones":         len(devoluciones),
        "tasa_cancelacion":     round(len(canceladas) / max(total_ord, 1) * 100, 1),
        "pct_entrega":          round(len(entregadas) / max(total_ord, 1) * 100, 1),
        "compras_meta_total":   compras_meta,
        "clics_total":          clics,
        "ctr_promedio":         round(ctr_prom, 2),
        "cpc_promedio":         round(cpc_prom, 3),
        "caja_neta":            round(caja_neta, 2),
        "dias_pauta_restantes": round(dias_pauta, 1),
        "gasto_diario_ads":     round(gasto_diario, 2),
    }


# ── PERSISTENCIA ──────────────────────────────────────────────────────────

def save_to_db(fecha_reporte, periodo_inicio, periodo_fin, df, meta, metrics, db_path=None) -> int:
    path = str(db_path or DB_PATH)
    conn = sqlite3.connect(path)
    c    = conn.cursor()

    c.execute(
        "INSERT OR IGNORE INTO semanas (fecha_reporte, periodo_inicio, periodo_fin) VALUES (?,?,?)",
        (fecha_reporte, periodo_inicio, periodo_fin),
    )
    conn.commit()
    c.execute("SELECT id FROM semanas WHERE fecha_reporte = ?", (fecha_reporte,))
    semana_id = c.fetchone()[0]

    for tabla in ["ordenes_dropi", "meta_ads_diario", "metricas_semanales", "transportadoras_semanales"]:
        c.execute(f"DELETE FROM {tabla} WHERE semana_id = ?", (semana_id,))

    # Helper para recuperar columna normalizada del df
    def gcol(key):
        norm_key = _norm(key).upper()
        return norm_key if norm_key in df.columns else key

    VENTA  = gcol("VALOR DE COMPRA EN PRODUCTOS")
    COSTO  = gcol("TOTAL EN PRECIOS DE PROVEEDOR")
    FLETE  = gcol("PRECIO FLETE")
    DEVFLT = gcol("COSTO DEVOLUCION FLETE")
    GANAN  = gcol("GANANCIA")
    CIUDAD = gcol("CIUDAD DESTINO")
    DEPTO  = gcol("DEPARTAMENTO DESTINO")
    CATS   = gcol("CATEGORIAS")
    TENVIO = gcol("TIPO DE ENVIO")
    GUIA   = gcol("NUMERO GUIA")
    VFACT  = gcol("VALOR FACTURADO")

    for _, row in df.iterrows():
        c.execute(
            """INSERT INTO ordenes_dropi
            (semana_id, orden_id, fecha, cliente, tienda, estatus,
             valor_venta, costo_producto, flete, costo_devolucion, ganancia_dropi,
             transportadora, novedad, es_prueba,
             ciudad_destino, departamento, categorias, tipo_envio, numero_guia, valor_facturado)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                semana_id,
                str(row.get("ID", "")),
                str(row.get("FECHA", "")),
                str(row.get(gcol("NOMBRE CLIENTE"), "")),
                str(row.get("TIENDA", "")),
                str(row.get("ESTATUS", "")),
                float(row.get(VENTA, 0) or 0),
                float(row.get(COSTO, 0) or 0),
                float(row.get(FLETE, 0) or 0),
                float(row.get(DEVFLT, 0) or 0),
                float(row.get(GANAN, 0) or 0),
                str(row.get("TRANSPORTADORA", "")),
                str(row.get("NOVEDAD", "")),
                int(row.get("es_prueba", 0)),
                str(row.get(CIUDAD, "") or ""),
                str(row.get(DEPTO, "") or ""),
                str(row.get(CATS, "") or ""),
                str(row.get(TENVIO, "") or ""),
                str(row.get(GUIA, "") or ""),
                float(row.get(VFACT, 0) or 0),
            ),
        )

    # Meta ads diario
    camp_col = "Nombre de la campaña" if "Nombre de la campaña" in meta.columns else "Nombre de la campa\xf1a"
    adset_col = "Nombre del conjunto de anuncios"
    for _, row in meta.iterrows():
        c.execute(
            """INSERT INTO meta_ads_diario
            (semana_id, fecha, campana, adset, anuncio, producto,
             gasto_eur, compras_meta, valor_conv_eur,
             roas_meta, cpa_meta, clics, impresiones, alcance, frecuencia, cpm,
             ctr, cpc, carritos, video_25pct, video_50pct, avg_video_play)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                semana_id,
                str(row.get("Día", "")),
                str(row.get(camp_col, "")),
                str(row.get(adset_col, "")),
                str(row.get("Nombre del anuncio", "")),
                str(row.get("producto", "")),
                float(row.get("Importe gastado (EUR)", 0) or 0),
                float(row.get("Compras", 0) or 0),
                float(row.get("Valor de conversión de compras", 0) or 0),
                float(row.get("ROAS de compras", 0) or 0),
                float(row.get("Costo por compra", 0) or 0),
                int(float(row.get("Clics en el enlace", 0) or 0)),
                int(float(row.get("Impresiones", 0) or 0)),
                int(float(row.get("Alcance", 0) or 0)),
                float(row.get("Frecuencia", 0) or 0),
                float(row.get("CPM (costo por mil impresiones)", 0) or 0),
                float(row.get("CTR (porcentaje de clics en el enlace)", 0) or 0),
                float(row.get("CPC (costo por clic en el enlace)", 0) or 0),
                int(float(row.get("Artículos agregados al carrito", 0) or 0)),
                int(float(row.get("Reproducciones de video hasta el 25%", 0) or 0)),
                int(float(row.get("Reproducciones de video hasta el 50%", 0) or 0)),
                float(row.get("Tiempo promedio de reproducción del video", 0) or 0),
            ),
        )

    # Métricas semanales
    m = metrics
    c.execute(
        """INSERT INTO metricas_semanales
        (semana_id, ventas_confirmadas, ventas_pipeline, cogs, flete_total, costo_devoluciones,
         gasto_ads_eur, gasto_ads_mxn, tasa_eur_mxn, margen_bruto, margen_bruto_pct,
         utilidad_neta, utilidad_neta_pct, mer, aov, cpa_real, cpa_breakeven, roas_real,
         roas_meta_promedio, total_ordenes, ordenes_prueba, entregadas, canceladas_reales,
         en_camino, devoluciones, tasa_cancelacion, pct_entrega, compras_meta_total,
         clics_total, ctr_promedio, cpc_promedio, caja_neta, dias_pauta_restantes, gasto_diario_ads)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            semana_id,
            m["ventas_confirmadas"], m["ventas_pipeline"], m["cogs"], m["flete_total"],
            m["costo_devoluciones"], m["gasto_ads_eur"], m["gasto_ads_mxn"], m["tasa_eur_mxn"],
            m["margen_bruto"], m["margen_bruto_pct"], m["utilidad_neta"], m["utilidad_neta_pct"],
            m["mer"], m["aov"], m["cpa_real"], m["cpa_breakeven"], m["roas_real"],
            m["roas_meta_promedio"], m["total_ordenes"], m["ordenes_prueba"], m["entregadas"],
            m["canceladas_reales"], m["en_camino"], m["devoluciones"], m["tasa_cancelacion"],
            m["pct_entrega"], m["compras_meta_total"], m["clics_total"], m["ctr_promedio"],
            m["cpc_promedio"], m["caja_neta"], m["dias_pauta_restantes"], m["gasto_diario_ads"],
        ),
    )

    # Transportadoras
    df_real = df[df["es_prueba"] == 0]
    TRANSP = gcol("TRANSPORTADORA") if gcol("TRANSPORTADORA") in df_real.columns else "TRANSPORTADORA"
    if TRANSP in df_real.columns:
        for transp, g in df_real.groupby(TRANSP):
            total        = len(g)
            entregadas_n = (g["ESTATUS"] == "ENTREGADO").sum()
            canceladas_n = (g["ESTATUS"] == "CANCELADO").sum()
            nov_mask     = (
                g["NOVEDAD"].notna() & (g["NOVEDAD"] != "") & (g["NOVEDAD"].str.upper() != "NO")
            ) if "NOVEDAD" in g.columns else pd.Series([False] * len(g))
            novedades_n  = nov_mask.sum()
            dev_mask     = g["ESTATUS"].isin(DEV_ESTADOS)
            costo_h      = float(g[dev_mask][FLETE].sum() + g[dev_mask][DEVFLT].sum())
            c.execute(
                """INSERT INTO transportadoras_semanales
                (semana_id, transportadora, total, entregadas, canceladas, novedades, pct_entrega, costo_hundido)
                VALUES (?,?,?,?,?,?,?,?)""",
                (semana_id, str(transp), int(total), int(entregadas_n), int(canceladas_n),
                 int(novedades_n), round(entregadas_n / max(total, 1) * 100, 1), round(costo_h, 2)),
            )

    conn.commit()
    conn.close()
    print(f"✅  {fecha_reporte}  → semana_id {semana_id} | {m['total_ordenes']} órdenes | "
          f"${m['ventas_confirmadas']:,.0f} MXN | Meta €{m['gasto_ads_eur']:.2f}")
    return semana_id


# ── CARTERA ───────────────────────────────────────────────────────────────

def _subtipo_cartera(desc: str, tipo: str) -> str:
    d = str(desc).upper()
    # ENTRADAs
    if tipo == "ENTRADA":
        if "GANANCIA" in d and "DROPSHIPPER" in d:
            return "ganancia"
        if "REINTEGRO" in d or "RECARGA" in d or "CORRECCION" in d:
            return "reintegro"
        if "CAMBIO DE ESTATUS" in d:
            return "ajuste_estatus"
        return "ganancia"  # default ENTRADA
    # SALIDAs
    if "PETICI" in d and "RETIRO" in d:
        return "retiro"          # transferencia a banco personal
    if "COBRO DE FLETE INICIAL" in d or ("NUEVA ORDEN" in d and "COBRO" not in d):
        return "flete_inicial"
    if "COBRO DE DEVOLUCI" in d or "ENTREGA NO EFECTIVA" in d:
        return "devolucion"
    if "TRANSFERENCIA DE WALLET" in d:
        return "transferencia"
    if "RET. ADMIN" in d or ("ADMIN" in d and "DESCUENTO" in d):
        return "admin"
    if "NUEVA ORDEN" in d:
        return "flete_inicial"
    return "otro"


def load_cartera(filepath: str) -> pd.DataFrame:
    df = pd.read_excel(filepath, dtype=str)
    df.columns = [c.strip().upper() for c in df.columns]
    desc_col = next((c for c in df.columns if c.startswith("DESCRI")), None)
    if desc_col and desc_col != "DESCRIPCION":
        df = df.rename(columns={desc_col: "DESCRIPCION"})
    elif "DESCRIPCION" not in df.columns:
        df["DESCRIPCION"] = ""
    for col in ("MONTO", "MONTO PREVIO"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    df["TIPO"] = df.get("TIPO", pd.Series(dtype=str)).fillna("").str.upper().str.strip()
    df["ORDEN ID"] = df.get("ORDEN ID", pd.Series(dtype=str)).fillna("")
    df["NUMERO DE GUIA"] = df.get("NUMERO DE GUIA", pd.Series(dtype=str)).fillna("")
    df["CONCEPTO DE RETIRO"] = df.get("CONCEPTO DE RETIRO", pd.Series(dtype=str)).fillna("")
    df["subtipo"] = df.apply(lambda r: _subtipo_cartera(r["DESCRIPCION"], r["TIPO"]), axis=1)
    return df


def cartera_metrics(df: pd.DataFrame) -> dict:
    ganancias    = float(df[df["TIPO"] == "ENTRADA"]["MONTO"].sum())
    fletes       = float(df[df["subtipo"] == "flete_inicial"]["MONTO"].sum())
    retiros      = float(df[df["subtipo"] == "retiro"]["MONTO"].sum())
    devoluciones = float(df[df["subtipo"] == "devolucion"]["MONTO"].sum())
    transferencias = float(df[df["subtipo"] == "transferencia"]["MONTO"].sum())
    admin        = float(df[df["subtipo"] == "admin"]["MONTO"].sum())
    # Ordenar por fecha REAL (dayfirst=True) para encontrar la transacción más reciente
    df_sorted = df.copy()
    df_sorted["_fecha_dt"] = pd.to_datetime(df_sorted["FECHA"], dayfirst=True, errors="coerce")
    df_sorted = df_sorted.sort_values("_fecha_dt", ascending=False)
    first        = df_sorted.iloc[0]
    sign = 1 if first["TIPO"] == "ENTRADA" else -1
    saldo_actual = float(first["MONTO PREVIO"]) + sign * float(first["MONTO"])
    return {
        "saldo_actual":       round(saldo_actual, 2),
        "total_ganancias":    round(ganancias, 2),
        "fletes_cobrados":    round(fletes, 2),
        "retiros_realizados": round(retiros, 2),
        "cobros_devolucion":  round(devoluciones, 2),
        "transferencias":     round(transferencias, 2),
        "ajustes_admin":      round(admin, 2),
        "n_transacciones":    len(df),
    }


def save_cartera_to_db(df: pd.DataFrame, db_path=None) -> int:
    path = str(db_path or DB_PATH)
    conn = sqlite3.connect(path)
    c    = conn.cursor()
    inserted = 0
    for _, row in df.iterrows():
        c.execute(
            """INSERT OR REPLACE INTO cartera_dropi
            (transaccion_id, fecha, tipo, subtipo, monto, monto_previo,
             orden_id, numero_guia, descripcion, concepto_retiro)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (str(row.get("ID", "")), str(row.get("FECHA", "")), str(row.get("TIPO", "")),
             str(row.get("subtipo", "otro")), float(row.get("MONTO", 0)),
             float(row.get("MONTO PREVIO", 0)), str(row.get("ORDEN ID", "")),
             str(row.get("NUMERO DE GUIA", "")), str(row.get("DESCRIPCION", "")),
             str(row.get("CONCEPTO DE RETIRO", ""))),
        )
        inserted += 1
    conn.commit()
    conn.close()
    return inserted


# ── BULK INGEST HISTÓRICO ──────────────────────────────────────────────────

def bulk_ingest_from_raw(tasa: float = None, db_path=None) -> list:
    """
    Lee todos los archivos raw de dropi/ y meta/ y los ingesta por semana ISO.
    Seguro de ejecutar múltiples veces (INSERT OR IGNORE en semanas).
    """
    from db_setup import create_db
    create_db(db_path)

    tasa = tasa or TASA_EUR_MXN

    # ── Cargar todo Dropi ─────────────────────────────────────────────────
    dropi_frames = []
    for f in sorted(glob.glob(str(RAW_DROPI_DIR / "*.xlsx"))):
        try:
            dropi_frames.append(load_dropi(f))
            print(f"  📦 {Path(f).name}")
        except Exception as e:
            print(f"  ⚠️  Error {Path(f).name}: {e}")

    if not dropi_frames:
        print("❌ No se encontraron archivos Dropi en", RAW_DROPI_DIR)
        return []

    # keep='last' → para órdenes duplicadas entre archivos, gana el archivo más reciente
    # (los archivos están ordenados cronológicamente por nombre, el último tiene el status más actualizado)
    all_dropi = pd.concat(dropi_frames, ignore_index=True).drop_duplicates(subset=["ID"], keep="last")

    # ── Cargar todo Meta ──────────────────────────────────────────────────
    meta_frames = []
    for f in sorted(glob.glob(str(RAW_META_DIR / "*.csv"))):
        try:
            meta_frames.append(load_meta(f))
            print(f"  📣 {Path(f).name}")
        except Exception as e:
            print(f"  ⚠️  Error {Path(f).name}: {e}")

    all_meta = pd.concat(meta_frames, ignore_index=True) if meta_frames else pd.DataFrame()
    if not all_meta.empty and "Día" in all_meta.columns:
        all_meta = all_meta.drop_duplicates(
            subset=["Día", "Nombre de la campaña" if "Nombre de la campaña" in all_meta.columns else all_meta.columns[0], "Nombre del anuncio"]
        )

    # ── Agrupar por semana ISO (lunes) ───────────────────────────────────
    FECHA_COL = _find_col(all_dropi, "FECHA") or "FECHA"
    all_dropi["_fecha_dt"] = pd.to_datetime(all_dropi[FECHA_COL], errors="coerce")
    # Lunes de cada semana
    all_dropi["_lunes"] = all_dropi["_fecha_dt"].dt.to_period("W-SUN").apply(
        lambda p: p.start_time.date() if not pd.isna(p) else None
    )

    if not all_meta.empty and "Día" in all_meta.columns:
        all_meta["_fecha_dt"] = pd.to_datetime(all_meta["Día"], errors="coerce")
        all_meta["_lunes"] = all_meta["_fecha_dt"].dt.to_period("W-SUN").apply(
            lambda p: p.start_time.date() if not pd.isna(p) else None
        )

    semanas = sorted(all_dropi["_lunes"].dropna().unique())
    print(f"\n📅 {len(semanas)} semanas a procesar ({semanas[0]} → {semanas[-1]})\n")

    results = []
    for lunes in semanas:
        df_w = all_dropi[all_dropi["_lunes"] == lunes].copy()
        if not all_meta.empty and "_lunes" in all_meta.columns:
            meta_w = all_meta[all_meta["_lunes"] == lunes].copy()
        else:
            meta_w = pd.DataFrame()

        fecha_str     = str(lunes)
        periodo_fin   = str(lunes + timedelta(days=6))

        try:
            metrics   = calculate_metrics(df_w, meta_w, tasa)
            semana_id = save_to_db(fecha_str, fecha_str, periodo_fin, df_w, meta_w, metrics, db_path)
            results.append((fecha_str, semana_id, metrics))
        except Exception as e:
            print(f"  ❌ Error semana {fecha_str}: {e}")

    # ── Cartera ───────────────────────────────────────────────────────────
    for f in sorted(glob.glob(str(RAW_CARTERA_DIR / "*.xlsx"))):
        try:
            df_c = load_cartera(f)
            n = save_cartera_to_db(df_c, db_path)
            print(f"\n💳 Cartera {Path(f).name} → {n} movimientos")
        except Exception as e:
            print(f"\n⚠️  Cartera error {Path(f).name}: {e}")

    print(f"\n✅ Bulk ingest completo — {len(results)} semanas procesadas")
    return results


# ── MAIN ──────────────────────────────────────────────────────────────────

def _print_summary(fecha: str, m: dict):
    print(f"\n📊 RESUMEN {fecha}")
    print(f"  Órdenes:     {m['total_ordenes']} reales ({m['ordenes_prueba']} pruebas excl.)")
    print(f"  Ventas:      ${m['ventas_confirmadas']:,.2f} MXN")
    print(f"  Margen:      ${m['margen_bruto']:,.2f} ({m['margen_bruto_pct']}%)")
    print(f"  Utilidad:    ${m['utilidad_neta']:,.2f} MXN")
    mer_warn = "⚠️  PELIGRO" if m["mer"] < 2 else "✅"
    print(f"  MER:         {m['mer']:.2f} {mer_warn}")
    print(f"  CPA real:    ${m['cpa_real']:,.2f} (BE: ${m['cpa_breakeven']:,.2f})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bulk",  action="store_true", help="Ingestar todos los archivos raw")
    parser.add_argument("--dropi", help="Ruta Excel Dropi")
    parser.add_argument("--meta",  help="Ruta CSV Meta")
    parser.add_argument("--fecha", help="Fecha reporte YYYY-MM-DD")
    parser.add_argument("--tasa",  type=float, default=None)
    args = parser.parse_args()

    if args.bulk:
        print("🚀 Iniciando bulk ingest histórico...")
        bulk_ingest_from_raw(tasa=args.tasa)
    elif args.dropi and args.meta and args.fecha:
        from db_setup import create_db
        create_db()
        tasa = args.tasa or TASA_EUR_MXN
        df   = load_dropi(args.dropi)
        meta = load_meta(args.meta)
        m    = calculate_metrics(df, meta, tasa)
        fecha_dt = date.fromisoformat(args.fecha)
        save_to_db(args.fecha, args.fecha, (fecha_dt + timedelta(days=6)).isoformat(), df, meta, m)
        _print_summary(args.fecha, m)
    else:
        print("Usa --bulk para ingestar todos los archivos raw, o --dropi/--meta/--fecha para una semana.")
