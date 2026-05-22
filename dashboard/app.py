"""
dashboard/app.py — Ojo con el Trend · Dashboard 360
Ejecutar: streamlit run dashboard/app.py
"""

import streamlit as st
import sqlite3
import pandas as pd
import numpy as np
import sys
import os
import tempfile
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from config import (DB_PATH, BENCHMARKS, TASA_EUR_MXN, DEV_ESTADOS,
                    PIPELINE_EXCLUIDOS, RAW_DROPI_DIR, RAW_META_DIR, SALDO_DROPI_REAL,
                    DIAS_PEDIDO_PERDIDO)
from db_setup import create_db

# ── PAGE CONFIG ───────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Ojo con el Trend · 360",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)
create_db()

# ── SESSION STATE ─────────────────────────────────────────────────────────
DEFAULTS = {
    "show_pruebas": False,
    "confirm_pruebas": False,
    "f_producto": [],
    "f_carrier": [],
    "f_status": [],
    "tasa": TASA_EUR_MXN,
    "periodo": "Todo el historial",
    "caja_banco_debito": 0.0,
    "caja_banco_credito": 0.0,
    "caja_credito_favor": 0.0,
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── DB HELPERS ────────────────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

@st.cache_data(ttl=120)
def load_all_orders_raw() -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql(
        """SELECT orden_id, fecha, cliente, tienda, estatus, valor_venta, costo_producto, flete,
                  costo_devolucion, ganancia_dropi, transportadora, novedad, es_prueba,
                  ciudad_destino, departamento, categorias, tipo_envio, numero_guia
           FROM ordenes_dropi""", conn)
    conn.close()
    df["fecha_dt"] = pd.to_datetime(df["fecha"], dayfirst=True, errors="coerce")
    return df

@st.cache_data(ttl=120)
def load_all_meta_raw() -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql(
        """SELECT fecha, campana, adset, anuncio, producto,
                  gasto_eur, compras_meta, roas_meta, cpa_meta,
                  clics, impresiones, alcance, frecuencia, cpm,
                  ctr, cpc, carritos, video_25pct, video_50pct, avg_video_play
           FROM meta_ads_diario""", conn)
    conn.close()
    df["fecha_dt"] = pd.to_datetime(df["fecha"], errors="coerce")
    return df

@st.cache_data(ttl=120)
def load_metricas_semanales() -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql(
        """SELECT s.fecha_reporte, m.*
           FROM semanas s JOIN metricas_semanales m ON s.id = m.semana_id
           ORDER BY s.fecha_reporte""", conn)
    conn.close()
    df["fecha_dt"] = pd.to_datetime(df["fecha_reporte"])
    return df

@st.cache_data(ttl=120)
def load_cartera_df() -> pd.DataFrame:
    conn = get_conn()
    try:
        df = pd.read_sql("SELECT * FROM cartera_dropi ORDER BY fecha DESC", conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df

@st.cache_data(ttl=120)
def load_delivery_times() -> pd.DataFrame:
    """Calcula el tiempo REAL de entrega: fecha orden → fecha acreditación en cartera."""
    conn = get_conn()
    try:
        df_car = pd.read_sql(
            "SELECT orden_id, fecha AS fecha_cobro, monto FROM cartera_dropi WHERE subtipo='ganancia'",
            conn)
        df_ord = pd.read_sql(
            """SELECT orden_id, fecha AS fecha_orden, transportadora, valor_venta
               FROM ordenes_dropi WHERE es_prueba=0 AND estatus='ENTREGADO'""",
            conn)
        df_car["fecha_cobro_dt"] = pd.to_datetime(
            df_car["fecha_cobro"].str[:10], dayfirst=True, errors="coerce")
        df_ord["fecha_orden_dt"] = pd.to_datetime(
            df_ord["fecha_orden"], dayfirst=True, errors="coerce")
        merged = df_ord.merge(df_car, on="orden_id", how="inner")
        merged["dias_entrega"] = (merged["fecha_cobro_dt"] - merged["fecha_orden_dt"]).dt.days
        merged = merged[merged["dias_entrega"] >= 0]
    except Exception:
        merged = pd.DataFrame()
    conn.close()
    return merged

@st.cache_data(ttl=120)
def load_pipeline_full() -> pd.DataFrame:
    """Carga todas las órdenes activas (no finalizadas) con su edad en días."""
    conn = get_conn()
    df = pd.read_sql(
        """SELECT orden_id, fecha, tienda, estatus, valor_venta, costo_producto,
                  flete, ganancia_dropi, transportadora, numero_guia
           FROM ordenes_dropi WHERE es_prueba=0""", conn)
    conn.close()
    df["fecha_dt"] = pd.to_datetime(df["fecha"], dayfirst=True, errors="coerce")
    hoy = pd.Timestamp(date.today())
    df["dias_edad"] = (hoy - df["fecha_dt"]).dt.days
    _FINALES = {
        "CANCELADO", "ENTREGADO", "RECHAZADO",
        "DEVOLUCION", "DEVOLUCION EN PROCESO", "DEVOLUCION EN TRANSITO",
        "PAQUETE EN DEVOLUCION", "PARA DEVOLUCION",
        "EN CAMINO A CIUDAD DE ORIGEN DEVOLUCION",
        "EN CIUDAD DE DESTINO DEVOLUCION",
    }
    return df[~df["estatus"].isin(_FINALES)].copy()

# ── METRIC HELPERS ────────────────────────────────────────────────────────
def compute_pl(orders: pd.DataFrame, meta: pd.DataFrame, tasa: float) -> dict:
    real = orders[orders["es_prueba"] == 0]
    ent  = real[real["estatus"] == "ENTREGADO"]
    can  = real[real["estatus"] == "CANCELADO"]
    dev  = real[real["estatus"].isin(DEV_ESTADOS)]
    # PERDIDAS: órdenes pipeline >45 días sin confirmar → excluir del pipeline real
    _perdido_col = real["es_perdido"] if "es_perdido" in real.columns else pd.Series(0, index=real.index)
    perdidas = real[_perdido_col == 1]
    pip = real[~real["estatus"].isin(PIPELINE_EXCLUIDOS) & (_perdido_col != 1)]

    rev   = float(ent["valor_venta"].sum())
    cogs  = float(ent["costo_producto"].sum())
    flete = float(ent["flete"].sum())
    mb    = rev - cogs - flete
    devol = float(dev["costo_devolucion"].sum() + dev["flete"].sum())
    geur  = float(meta["gasto_eur"].sum()) if not meta.empty else 0.0
    gmxn  = geur * tasa
    util  = mb - devol - gmxn

    n_ent = max(len(ent), 1)
    aov_s = real[real["estatus"] != "CANCELADO"]["valor_venta"]
    aov   = float(aov_s.mean()) if len(aov_s) else 0.0
    mer   = rev / max(gmxn, 1)

    return {
        "revenue":        round(rev, 2),
        "cogs":           round(cogs, 2),
        "flete":          round(flete, 2),
        "margen_bruto":   round(mb, 2),
        "mb_pct":         round(mb / max(rev, 1) * 100, 1),
        "costo_devol":    round(devol, 2),
        "gasto_eur":      round(geur, 2),
        "gasto_mxn":      round(gmxn, 2),
        "utilidad":       round(util, 2),
        "util_pct":       round(util / max(rev, 1) * 100, 1),
        "mer":            round(mer, 3),
        "roas_real":      round(rev / max(gmxn, 1), 3),
        "aov":            round(aov, 2),
        "cpa_real":       round(gmxn / n_ent, 2),
        "cpa_be":         round(aov - cogs / n_ent - flete / n_ent, 2),
        "n_total":        len(real),
        "n_entregadas":   len(ent),
        "n_canceladas":   len(can),
        "n_devol":        len(dev),
        "n_pipeline":     len(pip),
        "n_perdidas":     len(perdidas),
        "pct_entrega":    round(len(ent) / max(len(real), 1) * 100, 1),
        "pct_cancel":     round(len(can) / max(len(real), 1) * 100, 1),
        "pipeline_rev":   round(float(pip["valor_venta"].sum()), 2),
        "perdidas_rev":   round(float(perdidas["valor_venta"].sum()), 2),
    }

def semaforo(val, good, bad, higher=True):
    if higher:
        return "🟢" if val >= good else ("🟡" if val >= bad else "🔴")
    return "🟢" if val <= good else ("🟡" if val <= bad else "🔴")

def fmt(v, prefix="$", suffix=" MXN", decimals=0):
    if decimals == 0:
        return f"{prefix}{v:,.0f}{suffix}"
    return f"{prefix}{v:,.{decimals}f}{suffix}"

def cartera_resumen(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    ganancias  = float(df[df["tipo"] == "ENTRADA"]["monto"].sum())
    fletes     = float(df[df["subtipo"] == "flete_inicial"]["monto"].sum())
    retiros    = float(df[df["subtipo"] == "retiro"]["monto"].sum())
    devol      = float(df[df["subtipo"] == "devolucion"]["monto"].sum())
    transf     = float(df[df["subtipo"] == "transferencia"]["monto"].sum())
    adm        = float(df[df["subtipo"] == "admin"]["monto"].sum())
    # Ordenar por fecha real antes de tomar la más reciente (evita bug con DD-MM-YYYY como string)
    df_s   = df.copy()
    df_s["_fdt"] = pd.to_datetime(df_s["fecha"], dayfirst=True, errors="coerce")
    df_s   = df_s.sort_values("_fdt", ascending=False)
    first  = df_s.iloc[0]
    sign   = 1 if first["tipo"] == "ENTRADA" else -1
    saldo  = float(first["monto_previo"]) + sign * float(first["monto"])
    # Override con saldo real si está configurado y la diferencia > 5%
    saldo_real = SALDO_DROPI_REAL
    if saldo_real > 0 and abs(saldo - saldo_real) / max(saldo_real, 1) > 0.05:
        saldo = saldo_real
    return {"saldo_actual": round(saldo, 2), "total_ganancias": round(ganancias, 2),
            "fletes_cobrados": round(fletes, 2), "retiros_realizados": round(retiros, 2),
            "cobros_devolucion": round(devol, 2), "transferencias": round(transf, 2),
            "ajustes_admin": round(adm, 2), "n_transacciones": len(df)}


# ── SIDEBAR ───────────────────────────────────────────────────────────────
with st.sidebar:
    st.image("https://img.icons8.com/color/96/combo-chart.png", width=55)
    st.title("Ojo con el Trend")
    st.caption("Dashboard 360 · CDO Intelligence")
    st.divider()

    # ── Filtros globales ──────────────────────────────────────────────────
    st.subheader("🔍 Filtros globales")

    all_orders_raw = load_all_orders_raw()
    all_meta_raw   = load_all_meta_raw()
    cartera_df     = load_cartera_df()
    cartera_res    = cartera_resumen(cartera_df)

    # ── Flag PERDIDO: pipeline >45 días sin confirmar ─────────────────────
    _hoy = pd.Timestamp(date.today())
    _dias_old = (_hoy - all_orders_raw["fecha_dt"]).dt.days.fillna(999)
    _perdido_mask = (
        ~all_orders_raw["estatus"].isin(PIPELINE_EXCLUIDOS) &
        (_dias_old > DIAS_PEDIDO_PERDIDO) &
        (all_orders_raw["es_prueba"] == 0)
    )
    all_orders_raw = all_orders_raw.copy()
    all_orders_raw["es_perdido"] = _perdido_mask.astype(int)

    # Rango de fechas
    min_d = all_orders_raw["fecha_dt"].min().date() if not all_orders_raw.empty else date(2025, 12, 1)
    max_d = date.today()   # siempre permite hasta hoy, sin importar la última orden en DB
    date_range = st.date_input("Período", value=(min_d, max_d),
                               min_value=min_d, max_value=max_d)
    if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        d_start, d_end = date_range
    else:
        d_start, d_end = min_d, max_d

    # Filtros de dimensión
    productos_disp = sorted(all_meta_raw["producto"].dropna().unique().tolist())
    carriers_disp  = sorted(all_orders_raw[all_orders_raw["es_prueba"]==0]["transportadora"].dropna().unique().tolist())
    estados_disp   = sorted(all_orders_raw[all_orders_raw["es_prueba"]==0]["estatus"].dropna().unique().tolist())

    sel_prod    = st.multiselect("🛍️ Filtrar por producto", productos_disp,
                                default=st.session_state.get("f_producto", []),
                                placeholder="Todos", key="ms_prod")
    sel_carrier = st.multiselect("🚚 Filtrar por carrier", carriers_disp,
                                 default=st.session_state.get("f_carrier", []),
                                 placeholder="Todas", key="ms_carrier")
    sel_estado  = st.multiselect("📋 Filtrar por estado de orden", estados_disp,
                                 default=st.session_state.get("f_status", []),
                                 placeholder="Todos", key="ms_estado")
    # Sincronizar con session_state para que los botones de navegación funcionen
    st.session_state.f_producto = sel_prod
    st.session_state.f_carrier  = sel_carrier
    st.session_state.f_status   = sel_estado

    tasa = st.number_input("💱 Tipo de cambio EUR→MXN", value=st.session_state.tasa,
                           step=0.1, format="%.2f", key="tasa_input")
    st.session_state.tasa = tasa

    if st.button("✖ Limpiar filtros", use_container_width=True):
        st.session_state.f_producto = []
        st.session_state.f_carrier  = []
        st.session_state.f_status   = []
        st.rerun()

    st.divider()

    # ── Toggle pruebas con fricción ───────────────────────────────────────
    st.subheader("🧪 Órdenes de prueba")
    if st.session_state.show_pruebas:
        st.warning("⚠️ **MODO PRUEBAS ACTIVO**")
        if st.button("✖ Desactivar", use_container_width=True):
            st.session_state.show_pruebas = False
            st.session_state.confirm_pruebas = False
            st.rerun()
    elif st.session_state.confirm_pruebas:
        st.warning("¿Seguro? Contamina métricas.")
        c1, c2 = st.columns(2)
        if c1.button("Confirmar", type="primary", use_container_width=True):
            st.session_state.show_pruebas = True
            st.session_state.confirm_pruebas = False
            st.rerun()
        if c2.button("Cancelar", use_container_width=True):
            st.session_state.confirm_pruebas = False
            st.rerun()
    else:
        if st.button("Ver órdenes de prueba", use_container_width=True):
            st.session_state.confirm_pruebas = True
            st.rerun()

    st.divider()

    # ── Carga de datos ────────────────────────────────────────────────────
    st.subheader("📥 Cargar semana")
    fecha_rep    = st.date_input("Fecha del reporte", value=date.today())
    dropi_file   = st.file_uploader("📦 Dropi (.xlsx)", type=["xlsx"])
    meta_file    = st.file_uploader("📣 Meta Ads (.csv)", type=["csv"])
    cartera_file = st.file_uploader("💳 Cartera (.xlsx)", type=["xlsx"])

    run_btn = st.button("🚀 Procesar semana",
                        disabled=(dropi_file is None or meta_file is None),
                        type="primary", use_container_width=True)

    cartera_btn = st.button("💳 Importar cartera solo",
                             disabled=(cartera_file is None), use_container_width=True)

    if cartera_btn and cartera_file:
        with st.spinner("Importando cartera..."):
            try:
                from ingest import load_cartera, save_cartera_to_db
                with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as t:
                    t.write(cartera_file.read())
                    cp = t.name
                df_c = load_cartera(cp)
                os.unlink(cp)
                n = save_cartera_to_db(df_c)
                st.success(f"✅ {n} movimientos importados")
                st.cache_data.clear()
                st.rerun()
            except Exception as e:
                st.error(f"❌ {e}")

    if run_btn and dropi_file and meta_file:
        with st.spinner("Procesando..."):
            try:
                from ingest import load_dropi, load_meta, calculate_metrics, save_to_db
                with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as t:
                    t.write(dropi_file.read()); dp = t.name
                with tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="wb") as t:
                    t.write(meta_file.read()); mp = t.name
                df_d = load_dropi(dp); df_m = load_meta(mp)
                met  = calculate_metrics(df_d, df_m, tasa)
                fs   = fecha_rep.isoformat()
                pi   = (fecha_rep - timedelta(days=28)).isoformat()
                save_to_db(fs, pi, fs, df_d, df_m, met)
                os.unlink(dp); os.unlink(mp)
                if cartera_file:
                    from ingest import load_cartera, save_cartera_to_db
                    cartera_file.seek(0)
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as t:
                        t.write(cartera_file.read()); cp = t.name
                    save_cartera_to_db(load_cartera(cp))
                    os.unlink(cp)
                st.success(f"✅ Semana {fs} — {met['total_ordenes']} órdenes")
                st.cache_data.clear()
                st.rerun()
            except Exception as e:
                st.error(f"❌ {e}"); raise

    st.divider()

    # ── Bulk ingest ───────────────────────────────────────────────────────
    with st.expander("⚡ Ingestar archivos raw"):
        st.caption("Procesa todos los archivos en data/raw/ de una vez")
        if st.button("🔄 Bulk ingest", use_container_width=True):
            with st.spinner("Procesando archivos históricos..."):
                try:
                    from ingest import bulk_ingest_from_raw
                    results = bulk_ingest_from_raw(tasa=tasa)
                    st.success(f"✅ {len(results)} semanas procesadas")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ {e}")

    st.divider()

    # ── Caja externa ──────────────────────────────────────────────────────
    st.subheader("🏦 Caja externa")
    st.caption("Registra tus saldos fuera de Dropi")
    caja_debito = st.number_input(
        "Banco débito (MXN)", min_value=0.0, step=100.0, format="%.2f",
        value=st.session_state.caja_banco_debito, key="caja_debito_input")
    caja_credito_lim = st.number_input(
        "Crédito disponible (MXN)", min_value=0.0, step=100.0, format="%.2f",
        value=st.session_state.caja_banco_credito, key="caja_credito_lim_input")
    caja_credito_fav = st.number_input(
        "Saldo a favor tarjeta (MXN)", min_value=0.0, step=100.0, format="%.2f",
        value=st.session_state.caja_credito_favor, key="caja_credito_fav_input")
    st.session_state.caja_banco_debito  = caja_debito
    st.session_state.caja_banco_credito = caja_credito_lim
    st.session_state.caja_credito_favor = caja_credito_fav


# ── APLICAR FILTROS A LOS DATOS ───────────────────────────────────────────
orders = all_orders_raw.copy()
orders = orders[
    (orders["fecha_dt"] >= pd.Timestamp(d_start)) &
    (orders["fecha_dt"] <= pd.Timestamp(d_end))
]
if not st.session_state.show_pruebas:
    orders = orders[orders["es_prueba"] == 0]
if sel_carrier:
    orders = orders[orders["transportadora"].isin(sel_carrier)]
if sel_estado:
    orders = orders[orders["estatus"].isin(sel_estado)]

meta = all_meta_raw.copy()
meta = meta[
    (meta["fecha_dt"] >= pd.Timestamp(d_start)) &
    (meta["fecha_dt"] <= pd.Timestamp(d_end))
]
if sel_prod:
    meta = meta[meta["producto"].isin(sel_prod)]

# ── Mapeo producto Meta → tienda Dropi ───────────────────────────────────
# Cuando filtras por producto en Meta, también filtramos las órdenes
# por la tienda Dropi correspondiente para que el header y las métricas cuadren.
_PROD_TIENDA: dict[str, str] = {
    "Ashwagandha":                          "ANTI-ESTRES PRO",
    "Ashwaganha":                           "ANTI-ESTRES PRO",
    "Shilajit (ambas presentaciones)":      "ANTI-ESTRES PRO",
    "Shilajit Gomas 60 Pcs 3000mg":         "ANTI-ESTRES PRO",
    "FlyNew Shilajit Ultra: Ultimate Potency": "ANTI-ESTRES PRO",
    # Agrega aquí nuevos productos cuando los lances
}
if sel_prod and "tienda" in orders.columns:
    _tiendas_sel = {_PROD_TIENDA[p] for p in sel_prod if p in _PROD_TIENDA}
    if _tiendas_sel:
        orders = orders[orders["tienda"].isin(_tiendas_sel)]

semanas_df = load_metricas_semanales()

pl = compute_pl(orders, meta, tasa)

# ── Período anterior (WoW / comparación) ─────────────────────────────────
_period_days = max((pd.Timestamp(d_end) - pd.Timestamp(d_start)).days, 1)
_prev_end    = pd.Timestamp(d_start) - timedelta(days=1)
_prev_start  = _prev_end - timedelta(days=_period_days - 1)
_prev_orders = all_orders_raw[
    (all_orders_raw["fecha_dt"] >= _prev_start) &
    (all_orders_raw["fecha_dt"] <= _prev_end)
]
if not st.session_state.show_pruebas:
    _prev_orders = _prev_orders[_prev_orders["es_prueba"] == 0]
if sel_carrier:
    _prev_orders = _prev_orders[_prev_orders["transportadora"].isin(sel_carrier)]
_prev_meta = all_meta_raw[
    (all_meta_raw["fecha_dt"] >= _prev_start) &
    (all_meta_raw["fecha_dt"] <= _prev_end)
]
if sel_prod:
    _prev_meta = _prev_meta[_prev_meta["producto"].isin(sel_prod)]
pl_prev = compute_pl(_prev_orders, _prev_meta, tasa)

def _delta(curr, prev):
    """Retorna delta formateado para st.metric."""
    d = curr - prev
    return f"{d:+,.0f}" if abs(d) >= 1 else None

n_pruebas_excl = int(all_orders_raw[
    (all_orders_raw["fecha_dt"] >= pd.Timestamp(d_start)) &
    (all_orders_raw["fecha_dt"] <= pd.Timestamp(d_end)) &
    (all_orders_raw["es_prueba"] == 1)
].shape[0])

# ── BANNER SUPERIOR ───────────────────────────────────────────────────────
if st.session_state.show_pruebas:
    st.markdown('<div style="background:#fff3cd;border:2px solid #ffc107;padding:8px 14px;'
                'border-radius:6px;margin-bottom:6px;font-weight:bold">'
                '🟡 MODO PRUEBAS ACTIVO — datos no válidos para análisis</div>',
                unsafe_allow_html=True)

mer_ok  = pl["mer"] >= BENCHMARKS["mer_peligro"]
util_ok = pl["utilidad"] >= 0
bcolor  = "#ccffcc" if (mer_ok and util_ok) else ("#fff3cc" if util_ok else "#ffcccc")
icon    = "🟢" if (mer_ok and util_ok) else ("🟡" if util_ok else "🔴")
_saldo       = cartera_res.get("saldo_actual", 0) if cartera_res else 0
_caja_total  = (_saldo
                + st.session_state.caja_banco_debito
                + st.session_state.caja_banco_credito
                + st.session_state.caja_credito_favor)

active_filters = (
    [f"📅 {d_start}→{d_end}"] +
    [f"📦 {p}" for p in sel_prod] +
    [f"🚚 {c}" for c in sel_carrier]
)
chips = " ".join(
    f'<span style="background:#e0e7ff;color:#3730a3;padding:2px 8px;border-radius:10px;'
    f'font-size:0.78em;margin:0 3px">{f}</span>' for f in active_filters
)
_n_perdidas = pl.get("n_perdidas", 0)

# Build banner parts as a list to avoid blank lines from empty conditionals
# (blank lines inside an HTML block break Streamlit's markdown parser)
_negocio_estado = "BIEN 🎉" if (mer_ok and util_ok) else ("CUIDADO ⚠️" if util_ok else "MAL 🚨")
_negocio_color  = "#166534" if (mer_ok and util_ok) else ("#92400e" if util_ok else "#991b1b")
_saldo_str      = f'&nbsp;|&nbsp;💳 Saldo Dropi: <b>${_saldo:,.0f}</b>' if _saldo else ''
_caja_str       = f'&nbsp;|&nbsp;🏦 Caja: <b>${_caja_total:,.0f}</b>' if _caja_total else ''
_perdidas_str   = (f'&nbsp;|&nbsp;<span style="color:#dc2626;font-weight:bold">⚰️ {_n_perdidas} PERDIDAS</span>'
                   if _n_perdidas > 0 else '')

st.markdown(
    f'<div style="background:{bcolor};padding:14px 20px;border-radius:12px;margin-bottom:10px">'
    f'<div style="font-size:1.5em;font-weight:900;color:{_negocio_color};line-height:1.2">'
    f'🚦 Tu negocio está <span style="font-size:1.1em">{_negocio_estado}</span></div>'
    f'<div style="font-size:.9em;margin-top:6px;color:#374151">'
    f'💰 Revenue: <b>{fmt(pl["revenue"])}</b>'
    f'&nbsp;|&nbsp;📈 Utilidad: <b>{fmt(pl["utilidad"])}</b>'
    f'&nbsp;|&nbsp;⚡ MER: <b>{pl["mer"]:.2f}×</b> {"✅" if mer_ok else "⚠️"}'
    f'&nbsp;|&nbsp;📦 Entregas: <b>{pl["pct_entrega"]:.1f}%</b>'
    f'{_saldo_str}{_caja_str}{_perdidas_str}'
    f'&nbsp;|&nbsp;<span style="opacity:.7;font-size:.85em">🧪 {n_pruebas_excl} pruebas excl.</span>'
    f'</div>'
    f'<div style="margin-top:7px">{chips}</div>'
    f'</div>',
    unsafe_allow_html=True,
)

# ── MÉTRICAS TOP con deltas WoW ───────────────────────────────────────────
k1, k2, k3, k4, k5, k6, k7 = st.columns(7)
k1.metric("💰 Revenue",    fmt(pl["revenue"], decimals=0),
          _delta(pl["revenue"],   pl_prev["revenue"]),
          help="Dinero total cobrado de pedidos ENTREGADOS. Las cancelaciones y devoluciones NO cuentan. "
               "Es la base de todo: sin revenue no hay negocio.")
k2.metric("📈 Utilidad",   fmt(pl["utilidad"], decimals=0),
          _delta(pl["utilidad"],  pl_prev["utilidad"]),
          delta_color="normal" if pl["utilidad"] >= pl_prev["utilidad"] else "inverse",
          help="Lo que te queda LIMPIO después de pagar todo: producto, envíos, anuncios y devoluciones. "
               "Si es negativo, gastas más de lo que ganas. Si es positivo, el negocio funciona.")
k3.metric("⚡ MER",        f"{pl['mer']:.2f}×",
          f"{pl['mer']-pl_prev['mer']:+.2f}×" if pl_prev["mer"] else None,
          help="Marketing Efficiency Ratio: por cada $1 que gastas en anuncios, ¿cuánto revenue real genera? "
               "MER 2× = por $1 en ads entran $2. Meta >2×, ideal >3×. "
               "Más confiable que el ROAS que reporta Facebook.")
k4.metric("🎯 ROAS Real",  f"{pl['roas_real']:.2f}×",
          f"{pl['roas_real']-pl_prev['roas_real']:+.2f}×" if pl_prev["roas_real"] else None,
          help="Return on Ad Spend calculado con datos reales de Dropi (no con lo que dice Meta). "
               "ROAS 2.5× mínimo para ser rentable en tu negocio. ROAS <1.5× = estás perdiendo.")
k5.metric("💸 CPA",        fmt(pl["cpa_real"], decimals=0),
          _delta(pl["cpa_real"],  pl_prev["cpa_real"]),
          delta_color="inverse" if pl["cpa_real"] > pl_prev["cpa_real"] else "normal",
          help=f"Costo Por Adquisición: cuánto gastaste en anuncios por cada pedido ENTREGADO. "
               f"Tu break-even es ~${pl['cpa_be']:,.0f} MXN. "
               f"Si el CPA sube del break-even, cada venta te cuesta dinero.")
k6.metric("📦 Entregas",   f"{pl['pct_entrega']:.1f}%",
          f"{pl['pct_entrega']-pl_prev['pct_entrega']:+.1f}%" if pl_prev["pct_entrega"] else None,
          help="De cada 100 pedidos que creas, ¿cuántos llegan al cliente y se cobran? "
               "Normal en México COD: 50-70%. <40% = hay un problema con carrier o producto. "
               ">70% = excelente.")
k7.metric("Ⱉ Perdidas" if pl.get("n_perdidas",0) else "❌ Cancels",
          f"{pl.get('n_perdidas',0)} órd." if pl.get("n_perdidas",0) else f"{pl['pct_cancel']:.1f}%",
          help=(f"${pl.get('perdidas_rev',0):,.0f} MXN en órdenes sin confirmar >{DIAS_PEDIDO_PERDIDO} días — "
                "probablemente no se cobrarán." if pl.get("n_perdidas",0)
                else f"% de pedidos cancelados ANTES de enviarse. "
                     f"Normal: <15%. >20% = el anuncio atrae clientes que no quieren comprar."))

st.markdown("---")

# ── SCORE DE SALUD DEL NEGOCIO (0-100) ───────────────────────────────────
def _health_score(pl_data: dict) -> tuple[int, str, str]:
    """Calcula un score 0-100 que resume la salud del negocio."""
    score = 0
    # ROAS: 0-25 pts
    roas = pl_data.get("roas_real", 0)
    score += min(25, max(0, int(roas / 3.0 * 25)))
    # MER: 0-25 pts
    mer = pl_data.get("mer", 0)
    score += min(25, max(0, int(mer / 3.0 * 25)))
    # Tasa de entrega: 0-25 pts
    pct_ent = pl_data.get("pct_entrega", 0)
    score += min(25, max(0, int(pct_ent / 80.0 * 25)))
    # Cancelación (inverso): 0-25 pts
    pct_can = pl_data.get("pct_cancel", 100)
    score += min(25, max(0, int((1 - pct_can / 30.0) * 25)))
    score = max(0, min(100, score))
    if score >= 70:
        return score, "🟢 NEGOCIO SANO", "#22c55e"
    elif score >= 45:
        return score, "🟡 EN CONSTRUCCIÓN", "#f59e0b"
    else:
        return score, "🔴 NECESITA ATENCIÓN", "#ef4444"

_health, _health_label, _health_color = _health_score(pl)
_health_bar = "█" * (_health // 5) + "░" * (20 - _health // 5)

st.markdown(f"""
<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;
            padding:10px 18px;margin-bottom:8px;display:flex;align-items:center;gap:16px">
  <div style="min-width:120px">
    <span style="font-size:.75em;color:#64748b">SCORE DE SALUD</span><br>
    <span style="font-size:1.8em;font-weight:900;color:{_health_color}">{_health}</span>
    <span style="font-size:.85em;color:#64748b">/100</span>
  </div>
  <div style="flex:1">
    <div style="font-family:monospace;font-size:1em;color:{_health_color};letter-spacing:1px">{_health_bar}</div>
    <div style="font-size:.8em;font-weight:bold;color:{_health_color};margin-top:2px">{_health_label}</div>
  </div>
  <div style="font-size:.75em;color:#64748b;max-width:320px">
    ROAS {pl['roas_real']:.1f}× &nbsp;|&nbsp; MER {pl['mer']:.1f}× &nbsp;|&nbsp;
    Entrega {pl['pct_entrega']:.0f}% &nbsp;|&nbsp; Cancelación {pl['pct_cancel']:.0f}%<br>
    <span style="opacity:.7">Score basado en ROAS + MER + entrega + cancelación. 100 = negocio perfecto.</span>
  </div>
</div>
<div style="font-size:.78em;color:#475569;margin-top:4px;margin-bottom:4px;padding:0 4px">
📊 <b>Cómo se calcula:</b> ROAS (qué tan bien rinden tus anuncios) + MER (cuánto ganas vs lo que gastas) + Entregas (% pedidos que llegan al cliente) + Cancelaciones (% que se cancelan antes de salir)
</div>
""", unsafe_allow_html=True)

# ── TABS ──────────────────────────────────────────────────────────────────
try:
    import plotly.graph_objects as go
    import plotly.express as px
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

(tab_estrategia, tab_dropi, tab_resumen, tab_fin, tab_ventas, tab_mkt, tab_prod,
 tab_logistica, tab_q, tab_cartera_tab, tab_ordenes) = st.tabs([
    "🎯 Estrategia", "🏪 Dropi", "📊 Resumen", "💰 Financiero", "📦 Ventas",
    "📣 Marketing", "🛍️ Productos", "🚚 Logística",
    "📅 Quarters", "💳 Cartera", "🗂 Órdenes",
])


# ═══════════════════════════════════════════════════════════════════════════
# TAB 0 — ESTRATEGIA (EL DROPSHIPPER 360)
# ═══════════════════════════════════════════════════════════════════════════
with tab_estrategia:

    # ── Helpers de lógica estratégica ─────────────────────────────────────
    def _verdict_mer(mer):
        if mer >= 3.0:
            return "🚀 ESCALAR AGRESIVO",   "#ccffcc", "MER excelente. Duplica presupuesto si el ROAS aguanta 48h."
        elif mer >= 2.0:
            return "✅ ESCALAR MODERADO",   "#d4edda", "MER saludable. Sube budget 20-25% y monitorea CPA."
        elif mer >= 1.5:
            return "⚠️ MANTENER",           "#fff3cc", "MER en zona de peligro. Congela el gasto, optimiza creativos."
        else:
            return "🔴 CORTAR GASTO",       "#ffcccc", "Estás perdiendo dinero. Pausa campañas hasta corregir."

    def _verdict_cancel(pct):
        if pct > 20:
            return "🔴 CRISIS DE CANCELACIÓN", "#ffcccc", "Pausa Meta YA. Confirma pedidos por WhatsApp antes de despachar."
        elif pct > 15:
            return "⚠️ CANCELACIÓN ALTA",      "#fff3cc", "Agrega un paso de confirmación pre-envío. Revisa copy del anuncio."
        elif pct > 8:
            return "🟡 VIGILAR",               "#fffbe6", "Dentro de rango, pero monitorea semana a semana."
        else:
            return "✅ BAJO CONTROL",           "#d4edda", "Tasa de cancelación saludable."

    def _verdict_entrega(pct):
        if pct >= 75:
            return "✅ EXCELENTE",   "#d4edda", "Logística funcionando bien. Mantén carriers actuales."
        elif pct >= 65:
            return "🟡 ACEPTABLE",  "#fffbe6", "Revisa carrier con peor tasa y negocia o sustituye."
        else:
            return "🔴 LOGÍSTICA ROTA", "#ffcccc", "Tasa crítica. Cambia carrier principal esta semana."

    def _verdict_campaign(roas, ctr, cpa_real, cpa_be, gasto, dias):
        if roas >= 2.5 and ctr >= 1.5 and cpa_real <= cpa_be:
            return "🚀 ESCALAR",    "#ccffcc", f"ROAS {roas:.2f}x y CPA OK. Sube budget 25-30% hoy."
        elif roas >= 1.8 and cpa_real <= cpa_be * 1.15:
            return "✅ MANTENER",   "#d4edda", "Rentable. Testea un nuevo creativo sin bajar el actual."
        elif roas >= 1.2 and dias < 4:
            return "⏳ ESPERAR",    "#e8f4ff", f"Solo {dias:.0f} días activa. Dale 2-3 días más antes de decidir."
        elif ctr < 0.8 and dias >= 3:
            return "🔴 APAGAR",     "#ffcccc", f"CTR {ctr:.2f}% muy bajo. Creativo muerto. Apágala hoy."
        elif roas < 1.0:
            return "🔴 APAGAR",     "#ffcccc", f"ROAS {roas:.2f}x — perdiendo dinero. Apaga inmediatamente."
        elif roas < 1.5:
            return "⚠️ REDUCIR",   "#fff3cc", "ROAS bajo. Recorta presupuesto 40% y evalúa en 3 días."
        else:
            return "👀 MONITOREAR", "#f0f4ff", "Métricas mixtas. Observa 48h antes de escalar o cortar."

    def _new_product_signal(prod_meta_df, mer, util):
        signals = []
        if not prod_meta_df.empty:
            top = prod_meta_df.sort_values("gasto_eur", ascending=False).iloc[0]
            pct_concentracion = top["gasto_eur"] / prod_meta_df["gasto_eur"].sum() * 100
            if pct_concentracion > 75:
                signals.append(("⚠️ CONCENTRACIÓN", f"{top['producto']} absorbe el {pct_concentracion:.0f}% del gasto. "
                                 "Si falla ese producto, caes entero. Lanza un segundo producto en paralelo."))
            roas_top = float(top.get("roas_meta", 0))
            if roas_top < 1.5 and pct_concentracion > 50:
                signals.append(("🔴 PRODUCTO AGOTADO", f"{top['producto']} tiene ROAS {roas_top:.2f}x con alta inversión. "
                                 "La audiencia está saturada. Urgente: nuevo ángulo creativo o nuevo producto."))
        if mer >= 2.5 and util > 5000:
            signals.append(("💡 MOMENTO DE EXPANSIÓN", f"MER {mer:.2f}x y utilidad positiva. "
                             "Caja saludable para testear un nuevo producto con €30-50/día de presupuesto inicial."))
        if not signals:
            signals.append(("✅ PRODUCTO ACTUAL OK", "No hay señales urgentes de diversificación. "
                             "Enfócate en escalar lo que ya funciona."))
        return signals

    # ── Datos de la semana más reciente ───────────────────────────────────
    st.markdown(f"### 🎯 Plan de acción · semana del {date.today().strftime('%d/%m/%Y')}")
    st.caption("Basado en todos los datos del período seleccionado. Actualiza el filtro de fecha para analizar una semana específica.")

    if orders.empty and meta.empty:
        st.info("No hay datos en el período seleccionado. Ajusta el filtro de fechas.")
    else:
        # Datos frescos para análisis
        _mer   = pl["mer"]
        _roas  = pl["roas_real"]
        _cpa   = pl["cpa_real"]
        _cpa_be = pl["cpa_be"]
        _pct_cancel = pl["pct_cancel"]
        _pct_ent    = pl["pct_entrega"]
        _util       = pl["utilidad"]
        _rev        = pl["revenue"]
        _gasto_mxn  = pl["gasto_mxn"]
        _n_ent      = pl["n_entregadas"]
        _n_pip      = pl["n_pipeline"]

        # ── SECCIÓN 1: Diagnóstico ejecutivo ──────────────────────────────
        st.markdown("---")
        st.markdown("#### 🩺 Diagnóstico ejecutivo")

        d1, d2, d3, d4 = st.columns(4)

        lbl_mer, col_mer, tip_mer   = _verdict_mer(_mer)
        lbl_can, col_can, tip_can   = _verdict_cancel(_pct_cancel)
        lbl_ent, col_ent, tip_ent   = _verdict_entrega(_pct_ent)
        dias_pauta = _gasto_mxn / max(pl.get("gasto_diario_ads", _gasto_mxn / 7), 1) if _gasto_mxn else 0

        def _diag_card(col, titulo, valor_str, label, color, tip):
            col.markdown(
                f'<div style="background:{color};border-radius:10px;padding:12px 14px;height:110px">'
                f'<div style="font-size:.75em;opacity:.7">{titulo}</div>'
                f'<div style="font-size:1.4em;font-weight:bold;margin:2px 0">{valor_str}</div>'
                f'<div style="font-size:.82em;font-weight:bold">{label}</div>'
                f'<div style="font-size:.73em;margin-top:4px;opacity:.85">{tip}</div>'
                f'</div>', unsafe_allow_html=True)

        _diag_card(d1, "MER del período",    f"{_mer:.2f}×",       lbl_mer, col_mer, tip_mer)
        _diag_card(d2, "% Cancelación",      f"{_pct_cancel:.1f}%",lbl_can, col_can, tip_can)
        _diag_card(d3, "% Entrega",          f"{_pct_ent:.1f}%",   lbl_ent, col_ent, tip_ent)

        _roas_col = "#ccffcc" if _roas >= 2.5 else ("#fff3cc" if _roas >= 1.5 else "#ffcccc")
        _roas_lbl = "✅ ROAS RENTABLE" if _roas >= 2.5 else ("⚠️ ROAS LÍMITE" if _roas >= 1.5 else "🔴 ROAS NEGATIVO")
        _roas_tip = "Estás ganando bien por cada peso invertido." if _roas >= 2.5 else \
                    "Rentable, pero ajustado. Optimiza CPA." if _roas >= 1.5 else \
                    "Por cada $1 invertido recuperas menos de $1.5. Optimiza antes de escalar."
        _diag_card(d4, "ROAS Real (MXN)",    f"{_roas:.2f}×",      _roas_lbl, _roas_col, _roas_tip)

        # ── SECCIÓN 2: Lista de acciones prioritarias ─────────────────────
        st.markdown("---")
        st.markdown("#### 🔥 Acciones prioritarias — hazlas en orden")

        acciones = []  # (prioridad 1-10, urgencia_emoji, titulo, detalle)

        # Reglas de negocio
        if _pct_cancel > 20:
            acciones.append((1, "🔴", "PAUSA META AHORA",
                "Cancelación >20%. Cada orden nueva que entra es probable pérdida. "
                "Pausa todas las campañas, llama a los clientes pendientes y corrige el problema antes de reactivar."))
        if _pct_ent < 60:
            acciones.append((2, "🔴", "CAMBIA DE CARRIER PRINCIPAL",
                f"Solo el {_pct_ent:.0f}% de entregas llegan. Estás regalando producto y flete. "
                "Habla HOY con AMPM o TIUI como alternativa. Cada día que sigues con el carrier actual pierdes dinero."))
        if _mer < 1.5 and _gasto_mxn > 0:
            acciones.append((3, "🔴", "REDUCE GASTO ADS AL MÍNIMO",
                f"MER {_mer:.2f}x — no estás cubriendo costos. Baja el presupuesto diario a €10-15 solo para mantener datos "
                "mientras optimizas el funnel. No escales con MER negativo."))
        if _cpa > _cpa_be * 1.1 and _gasto_mxn > 0:
            acciones.append((4, "🔴", "CPA SOBRE BREAK-EVEN",
                f"CPA real ${_cpa:,.0f} vs break-even ${_cpa_be:,.0f}. "
                "Cada venta que cierras con Meta te cuesta más de lo que te deja. Optimiza la landing, el precio o el creativo."))
        if _pct_cancel > 15:
            acciones.append((5, "⚠️", "ACTIVA CONFIRMACIÓN PRE-ENTREGA",
                "Envía un WhatsApp el día antes de cada entrega confirmando hora y dirección. "
                "Esto reduce cancelaciones 30-40% en COD México. Puedes hacerlo manual con los pedidos de hoy mismo."))
        if _n_pip > 20:
            acciones.append((6, "⚠️", f"SEGUIMIENTO A {_n_pip} ÓRDENES EN PIPELINE",
                f"Tienes ${pl['pipeline_rev']:,.0f} MXN en tránsito. "
                "Revisa en Dropi cuáles llevan más de 5 días sin movimiento y escala con la transportadora hoy."))
        if _mer >= 2.5 and _roas >= 2.0:
            acciones.append((7, "🚀", "ESCALA LAS CAMPAÑAS GANADORAS",
                f"MER {_mer:.2f}x y ROAS {_roas:.2f}x — estás en zona verde. "
                "Sube el budget de las campañas con ROAS >2.5x entre un 25-30%. No toques las que están debajo."))
        if _mer >= 2.0 and not any(a[2] == "ESCALA LAS CAMPAÑAS GANADORAS" for a in acciones):
            acciones.append((7, "✅", "SUBE BUDGET MODERADO",
                f"MER {_mer:.2f}x saludable. Aumenta el presupuesto de tus mejores campañas 15-20% "
                "y mide el CPA en las primeras 24h antes de subir más."))
        if not meta.empty:
            prod_conc = meta.groupby("producto")["gasto_eur"].sum()
            top_prod  = prod_conc.idxmax()
            pct_conc  = prod_conc.max() / prod_conc.sum() * 100
            if pct_conc > 80:
                acciones.append((8, "⚠️", f"DIVERSIFICA: {top_prod} TIENE EL {pct_conc:.0f}% DEL GASTO",
                    "Demasiada concentración en un solo producto. Si ese adset se satura o Meta lo penaliza, "
                    "caes entero. Lanza un segundo producto con €20-30/día esta semana."))
        if _util > 8000 and _mer >= 2.5:
            acciones.append((9, "💡", "USA LA UTILIDAD PARA TESTEAR NUEVO PRODUCTO",
                f"Tienes ${_util:,.0f} MXN de utilidad y MER sólido. "
                "Es el momento de invertir €50-80 en un nuevo producto. Criterios: ticket >$1,200 MXN, "
                "producto con problema claro, que se pueda mostrar en video de 15s."))
        if _pct_ent >= 75 and _pct_cancel <= 10 and _mer >= 2.0:
            acciones.append((10, "🏆", "NEGOCÍA MEJORES CONDICIONES CON DROPI",
                "Todo va bien: entrega alta, cancelación baja, MER rentable. "
                "Es el momento de pedir a Dropi mejores comisiones o acceso a productos exclusivos."))

        if not acciones:
            acciones.append((5, "✅", "MANTÉN EL RUMBO",
                "Métricas en rango. Monitorea diariamente y sube budget solo si el MER se mantiene >2.0x por 3 días seguidos."))

        acciones.sort(key=lambda x: x[0])

        for i, (_, urg, titulo, detalle) in enumerate(acciones, 1):
            bg = "#ffcccc" if urg == "🔴" else ("#fff3cc" if urg == "⚠️" else ("#d4edda" if urg in ("✅","🏆") else "#e8f4ff"))
            st.markdown(
                f'<div style="background:{bg};border-radius:10px;padding:13px 18px;margin-bottom:8px;'
                f'border-left:5px solid {"#ef4444" if urg=="🔴" else "#f59e0b" if urg=="⚠️" else "#22c55e" if urg in ("✅","🏆") else "#3b82f6"}">'
                f'<span style="font-size:1em;font-weight:bold">{i}. {urg} {titulo}</span><br>'
                f'<span style="font-size:.88em">{detalle}</span>'
                f'</div>', unsafe_allow_html=True)

        # ── SECCIÓN 3: Veredicto por campaña ──────────────────────────────
        if not meta.empty:
            st.markdown("---")
            st.markdown("#### 📊 Qué hacer con cada campaña")
            st.caption("Filtra el período a los últimos 7 días para un análisis de semana actual.")

            camp_strat = (meta.groupby("campana").agg(
                gasto=("gasto_eur",      "sum"),
                compras=("compras_meta", "sum"),
                roas=("roas_meta",       "mean"),
                ctr=("ctr",             "mean"),
                cpc=("cpc",             "mean"),
                dias=("fecha_dt",        "nunique"),
            ).reset_index())
            camp_strat["cpa_meta"] = camp_strat["gasto"] / camp_strat["compras"].clip(lower=0.01)

            rows_c = []
            for _, row in camp_strat.iterrows():
                lbl, col_c, tip_c = _verdict_campaign(
                    float(row["roas"]), float(row["ctr"]),
                    float(row["cpa_meta"]) * tasa, _cpa_be,
                    float(row["gasto"]), float(row["dias"]))
                rows_c.append({
                    "Campaña":   row["campana"][:55],
                    "Gasto €":   f"€{row['gasto']:.2f}",
                    "ROAS":      f"{row['roas']:.2f}×",
                    "CTR":       f"{row['ctr']:.2f}%",
                    "CPC €":     f"€{row['cpc']:.3f}",
                    "Días":      int(row["dias"]),
                    "Acción":    lbl,
                    "Qué hacer": tip_c,
                    "_color":    col_c,
                })

            import pandas as pd
            df_camp_strat = pd.DataFrame(rows_c)

            # Resaltado por color — extraer colores ANTES de drop
            _colors_c = df_camp_strat["_color"].tolist()
            _df_c_display = df_camp_strat.drop(columns="_color").reset_index(drop=True)
            def _highlight_accion(row):
                return [f"background-color:{_colors_c[row.name]}"] * len(row)

            st.dataframe(
                _df_c_display.style.apply(_highlight_accion, axis=1),
                hide_index=True, use_container_width=True)

        # ── SECCIÓN 4: Veredicto por carrier ──────────────────────────────
        if not orders.empty:
            st.markdown("---")
            st.markdown("#### 🚚 Qué hacer con cada transportadora")

            real_o = orders[orders["es_prueba"] == 0]
            carr_s = (real_o.groupby("transportadora").agg(
                total=("orden_id",  "count"),
                ent=("estatus",     lambda x: (x == "ENTREGADO").sum()),
                can=("estatus",     lambda x: (x == "CANCELADO").sum()),
                costo=("costo_devolucion", "sum"),
            ).reset_index())
            carr_s["pct_ent"] = carr_s["ent"] / carr_s["total"].clip(lower=1) * 100
            carr_s["pct_can"] = carr_s["can"] / carr_s["total"].clip(lower=1) * 100

            rows_k = []
            for _, row in carr_s.iterrows():
                pct = float(row["pct_ent"])
                if pct >= 75:
                    ac = "✅ MANTENER"
                    tip_k = "Buen rendimiento. Negocia descuento por volumen si mandas >50/sem."
                    ck = "#d4edda"
                elif pct >= 60:
                    ac = "⚠️ PRESIONAR"
                    tip_k = "Rendimiento medio. Habla con tu ejecutivo y exige mejora en 2 semanas o cambias."
                    ck = "#fff3cc"
                elif float(row["total"]) < 5:
                    ac = "⏳ MUESTRA CHICA"
                    tip_k = "Pocos envíos para concluir. Dale 10+ pedidos antes de juzgar."
                    ck = "#e8f4ff"
                else:
                    ac = "🔴 SUSTITUIR"
                    tip_k = "Tasa de entrega inaceptable. Migra pedidos a otro carrier esta semana."
                    ck = "#ffcccc"
                rows_k.append({
                    "Carrier":    row["transportadora"],
                    "Envíos":     int(row["total"]),
                    "Entregadas": int(row["ent"]),
                    "% Entrega":  f"{pct:.1f}%",
                    "% Cancel":   f"{float(row['pct_can']):.1f}%",
                    "Costo devol":f"${float(row['costo']):,.0f}",
                    "Acción":     ac,
                    "Qué hacer":  tip_k,
                    "_color":     ck,
                })

            df_carr_s = pd.DataFrame(rows_k)
            _colors_k = df_carr_s["_color"].tolist()
            _df_k_display = df_carr_s.drop(columns="_color").reset_index(drop=True)
            st.dataframe(
                _df_k_display.style.apply(
                    lambda row: [f"background-color:{_colors_k[row.name]}"] * len(row), axis=1),
                hide_index=True, use_container_width=True)

        # ── SECCIÓN 5: Señales de nuevo producto ──────────────────────────
        st.markdown("---")
        st.markdown("#### 💡 ¿Cuándo lanzar el próximo producto?")

        if not meta.empty:
            prod_meta_s = meta.groupby("producto").agg(
                gasto_eur=("gasto_eur",  "sum"),
                roas_meta=("roas_meta",  "mean"),
                compras=("compras_meta", "sum"),
            ).reset_index()
            signals = _new_product_signal(prod_meta_s, _mer, _util)
        else:
            signals = [("⚠️ SIN DATOS META", "Carga datos de Meta Ads para obtener análisis de productos.")]

        for sig_lbl, sig_txt in signals:
            sig_color = "#ffcccc" if "🔴" in sig_lbl else ("#fff3cc" if "⚠️" in sig_lbl else "#d4edda" if "✅" in sig_lbl else "#e8f4ff")
            st.markdown(
                f'<div style="background:{sig_color};border-radius:8px;padding:11px 16px;margin-bottom:6px">'
                f'<b>{sig_lbl}</b><br><span style="font-size:.88em">{sig_txt}</span></div>',
                unsafe_allow_html=True)

        # ── SECCIÓN 6: Proyección semana que viene ────────────────────────
        st.markdown("---")
        st.markdown("#### 📈 Proyección: si mantienes el ritmo actual…")

        semana_dias = max((pd.Timestamp(d_end) - pd.Timestamp(d_start)).days, 1)
        ordenes_dia  = pl["n_total"] / semana_dias
        rev_dia      = _rev / semana_dias
        gasto_dia    = _gasto_mxn / semana_dias
        util_dia     = _util / semana_dias

        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Órdenes / semana",  f"{ordenes_dia * 7:.0f}")
        p2.metric("Revenue / semana",  f"${rev_dia * 7:,.0f} MXN")
        p3.metric("Gasto Ads / semana",f"${gasto_dia * 7:,.0f} MXN")
        p4.metric("Utilidad / semana", f"${util_dia * 7:,.0f} MXN",
                  delta="📈" if util_dia > 0 else "📉")

        # Escenarios
        st.markdown("**¿Qué pasa si escalas?**")
        esc1, esc2, esc3 = st.columns(3)
        for col_e, mult, label in [(esc1, 1.25, "+25% budget"), (esc2, 1.5, "+50% budget"), (esc3, 2.0, "×2 budget")]:
            nuevo_gasto = gasto_dia * 7 * mult
            nuevo_rev   = rev_dia   * 7 * mult  # asumiendo ROAS se mantiene
            nueva_util  = nuevo_rev - (pl["cogs"] / semana_dias * 7 * mult) \
                          - (pl["flete"] / semana_dias * 7 * mult) \
                          - nuevo_gasto \
                          - (pl["costo_devol"] / semana_dias * 7 * mult)
            col_e.metric(
                label,
                f"${nueva_util:,.0f} MXN/sem",
                f"Ads: ${nuevo_gasto:,.0f}",
                delta_color="normal" if nueva_util > _util / semana_dias * 7 else "inverse")

        st.caption("⚠️ Proyección lineal asumiendo que el ROAS se mantiene constante al escalar — en la práctica el ROAS suele bajar al aumentar budget. Úsalo como referencia, no como garantía.")

        # ── SECCIÓN 7: Estrategia de suplementos ─────────────────────────
        st.markdown("---")
        st.markdown("#### 💊 Estrategia de Suplementos — ojoconeltrend.com")

        # Catálogo activo en Shopify
        st.markdown("**🛒 Activos en tienda ahora:**")
        shopify_activos = [
            ("Shilajit Gomas 60 Pcs", "$899", "SHILAJIT-GOMAS", "1,852 uds"),
            ("Flo Ovarian Support", "$999", "FLO-OVARIAN", "1,176 uds"),
            ("Inositol + Ashwagandha + Vit D", "$999", "SUPLEMENTO-INOSITOL", "487 uds"),
            ("Primal Queen Bienestar Mujer", "$1,099", "SUPLEMENTO-PRIMAL-QUEEN", "600 uds"),
            ("Goli Vinagre de Manzana Gomas", "$899", "SUPLEMENTO-GOLI-VINAGRE", "278 uds"),
        ]
        for prod_name, precio, sku, inv in shopify_activos:
            st.markdown(f"&nbsp;&nbsp;• **{prod_name}** — {precio} MXN &nbsp;|&nbsp; SKU: `{sku}` &nbsp;|&nbsp; Inv: {inv}", unsafe_allow_html=True)

        st.markdown("**📝 Drafts listos para lanzar:**")
        drafts_row = "Moringa $599 · Melena de León $799 · Turkesterone $899 · Zesty Debloat $799 · Cúrcuma $899 · Colágeno Hidrolizado $899 · Glucosamina $899 · Cardo Mariano $899 · Bloom $899"
        st.caption(drafts_row)

        st.markdown("")
        supl_cols = st.columns(3)
        supl_cards = [
            ("🧪 Próximo lanzamiento en Meta",
             "Citrato de Magnesio o Melatonina — audiencia masiva (sueño, estrés). "
             "€20/día, 2 creativos (ángulo sueño vs ángulo estrés). "
             "Si en 4 días ROAS > 8×, duplica budget.", "#e8f4ff"),
            ("📦 Bundle anti-estrés",
             "Shilajit Gomas + Inositol + Ashwagandha = stack mujer bienestar. "
             "Bundle en Shopify con 12-15% descuento. "
             "AOV objetivo: $1,400–1,800 MXN. Menos envíos, mejor MER.", "#d4edda"),
            ("⚠️ Calidad antes de escalar",
             "Cada draft nuevo: prueba 2-3 unidades tú mismo antes de lanzar en Meta. "
             "Etiqueta, textura, sabor, presentación. "
             "Un producto malo = riesgo cuenta Meta + devoluciones.", "#fff3cc"),
        ]
        for col_s, (titulo_s, texto_s, color_s) in zip(supl_cols, supl_cards):
            col_s.markdown(
                f'<div style="background:{color_s};border-radius:10px;padding:14px 16px;height:140px">'
                f'<div style="font-weight:bold;margin-bottom:6px">{titulo_s}</div>'
                f'<div style="font-size:.85em">{texto_s}</div>'
                f'</div>', unsafe_allow_html=True)

        # ── SECCIÓN 8: Navegación rápida / interactividad ─────────────────
        st.markdown("---")
        st.markdown("#### 🔗 Acciones rápidas")
        nav1, nav2, nav3 = st.columns(3)

        # Filtro rápido por carrier desde Estrategia
        if not orders.empty:
            carriers_strat = sorted(orders[orders["es_prueba"]==0]["transportadora"].dropna().unique().tolist())
            with nav1:
                st.caption("📦 Ver órdenes de un carrier")
                carrier_nav = st.selectbox("Carrier", ["—"] + carriers_strat, key="nav_carrier_strat")
                if st.button("🔍 Filtrar en Órdenes", key="btn_nav_carrier", use_container_width=True):
                    if carrier_nav != "—":
                        st.session_state.f_carrier = [carrier_nav]
                        st.rerun()

        # Filtro rápido por producto desde Estrategia
        if not meta.empty:
            prods_strat = sorted(meta["producto"].dropna().unique().tolist())
            with nav2:
                st.caption("📣 Ver datos de un producto")
                prod_nav = st.selectbox("Producto", ["—"] + prods_strat, key="nav_prod_strat")
                if st.button("🔍 Filtrar en Marketing", key="btn_nav_prod", use_container_width=True):
                    if prod_nav != "—":
                        st.session_state.f_producto = [prod_nav]
                        st.rerun()

        # PERDIDAS: descargar lista
        with nav3:
            st.caption(f"⚰️ Órdenes perdidas (>{DIAS_PEDIDO_PERDIDO} días)")
            if "es_perdido" in orders.columns:
                df_perdidas_exp = orders[orders["es_perdido"] == 1][
                    ["orden_id","fecha","cliente","estatus","valor_venta","transportadora","ciudad_destino"]
                ].rename(columns={"orden_id":"ID","fecha":"Fecha","cliente":"Cliente",
                                  "estatus":"Estatus","valor_venta":"Venta MXN",
                                  "transportadora":"Carrier","ciudad_destino":"Ciudad"})
                n_perd = len(df_perdidas_exp)
                if n_perd:
                    st.metric("Total perdidas", f"{n_perd} órdenes", f"${orders[orders['es_perdido']==1]['valor_venta'].sum():,.0f} MXN")
                    st.download_button(
                        "⬇️ Descargar CSV perdidas", df_perdidas_exp.to_csv(index=False).encode(),
                        file_name=f"ordenes_perdidas_{date.today()}.csv", mime="text/csv",
                        use_container_width=True)
                else:
                    st.success(f"✅ Sin órdenes perdidas en el período")


# ═══════════════════════════════════════════════════════════════════════════
# TAB DROPI — TODO LO QUE PASA EN DROPI, EXPLICADO SIMPLE
# ═══════════════════════════════════════════════════════════════════════════
with tab_dropi:
    st.markdown("## 🏪 Tu negocio en Dropi")
    st.caption("Todo explicado como si tuvieras 5 años. Sin palabras raras. 👶")

    # ── Datos de cartera ────────────────────────────────────────────────
    saldo_actual      = cartera_res.get("saldo_actual", 0)      if cartera_res else 0
    total_ganancias   = cartera_res.get("total_ganancias", 0)   if cartera_res else 0
    fletes_pagados    = cartera_res.get("fletes_cobrados", 0)   if cartera_res else 0
    retiros_realizados = cartera_res.get("retiros_realizados", 0) if cartera_res else 0
    cobros_devolucion = cartera_res.get("cobros_devolucion", 0) if cartera_res else 0
    n_movs            = cartera_res.get("n_transacciones", 0)   if cartera_res else 0

    # ── BLOQUE 1: ¿Qué es Dropi? ────────────────────────────────────────
    st.markdown("""
    <div style="background:#e8f4ff;border-radius:12px;padding:16px 20px;margin-bottom:18px">
    <b>👶 ¿Qué es Dropi?</b><br><br>
    Dropi es como una tienda de bodega que guarda tu producto, lo empaca y lo manda al cliente cuando alguien compra.
    Cuando el cliente <i>recibe</i> el paquete, Dropi te acredita tu ganancia en una alcancía virtual (la <b>cartera</b>).
    Tú puedes sacar ese dinero cuando quieras (eso se llama <b>retiro</b>).
    </div>
    """, unsafe_allow_html=True)

    # ── BLOQUE 2: Tu dinero ahora mismo ─────────────────────────────────
    st.markdown("### 💳 Tu dinero en Dropi ahora mismo")
    d_a, d_b, d_c, d_d = st.columns(4)
    d_a.metric("💳 Saldo en Dropi",         f"${saldo_actual:,.0f} MXN",
               help="Lo que tienes acumulado en la cartera de Dropi. Puedes pedirlo a tu banco cuando quieras.")
    d_b.metric("📥 Total que te han pagado", f"${total_ganancias:,.0f} MXN",
               help="Todo lo que Dropi te ha acreditado por pedidos entregados desde el inicio.")
    d_c.metric("📤 Total que has retirado",  f"${retiros_realizados:,.0f} MXN",
               help="El dinero que ya sacaste a tu banco.")
    d_d.metric("↩️ Cobros por devolución",  f"${cobros_devolucion:,.0f} MXN",
               help="Lo que te han cobrado cuando un cliente regresa el producto.")

    st.markdown(f"""
    <div style="background:#f0fdf4;border-left:4px solid #22c55e;border-radius:8px;padding:14px 18px;margin:10px 0">
    <b>📖 En palabras sencillas:</b><br>
    Dropi te ha pagado <b>${total_ganancias:,.0f} MXN</b> en total por tus entregas.
    De eso, ya te llevaste <b>${retiros_realizados:,.0f} MXN</b> a tu banco.
    En la alcancía de Dropi te quedan <b>${saldo_actual:,.0f} MXN</b>.
    Pero ojo: también te han cobrado <b>${fletes_pagados:,.0f} MXN</b> en fletes (el costo del mensajero
    cuando generas una guía) y <b>${cobros_devolucion:,.0f} MXN</b> cuando te devolvieron productos.
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # ── BLOQUE 3: ¿Estoy ganando o perdiendo? ───────────────────────────
    st.markdown("### 🤔 ¿Mi negocio es rentable?")
    rev     = pl["revenue"]
    util    = pl["utilidad"]
    gasto_ads = pl["gasto_mxn"]
    n_ent   = pl["n_entregadas"]
    n_tot   = pl["n_total"]
    n_pip   = pl["n_pipeline"]
    pct_ent = pl["pct_entrega"]
    pct_can = pl["pct_cancel"]

    if util > 0:
        ren_color  = "#ccffcc"; ren_emoji = "🟢"
        ren_titulo = "¡SÍ! Estás ganando dinero en el período seleccionado"
        ren_texto  = (f"Vendiste <b>${rev:,.0f} MXN</b> en productos entregados. "
                      f"Después de pagar el costo del producto, los envíos, las devoluciones "
                      f"y los anuncios de Facebook/Instagram, te quedan "
                      f"<b>${util:,.0f} MXN</b> de ganancia limpia. 🎉")
    elif rev > 0 and n_ent > 0:
        ren_color  = "#fff3cc"; ren_emoji = "🟡"
        ren_titulo = "CASI — Vendes, pero los anuncios cuestan más de lo que ganas"
        gasto_extra = abs(util)
        ren_texto  = (f"Vendiste <b>${rev:,.0f} MXN</b>, lo cual es bueno. "
                      f"Pero gastaste <b>${gasto_ads:,.0f} MXN</b> en anuncios y al sumar todos los costos "
                      f"perdiste <b>${gasto_extra:,.0f} MXN</b>. "
                      f"No es que el negocio esté roto — es que el costo de conseguir cada cliente "
                      f"es demasiado alto. La solución: mejorar los anuncios o subir el precio.")
    elif n_tot > 0:
        ren_color  = "#fff0e0"; ren_emoji = "🟠"
        ren_titulo = "TODAVÍA NO — Hay pedidos pero pocos se han entregado"
        ren_texto  = (f"Tienes {n_tot} órdenes, pero solo {n_ent} se han entregado. "
                      f"Muchas están en camino ({n_pip} en pipeline). "
                      f"El negocio está arrancando. Cuando más pedidos lleguen al cliente, "
                      f"la ganancia en Dropi subirá.")
    else:
        ren_color  = "#ffcccc"; ren_emoji = "🔴"
        ren_titulo = "NO — No hay ventas entregadas en el período seleccionado"
        ren_texto  = "Ajusta el filtro de fechas en la barra lateral para ver períodos con ventas."

    st.markdown(f"""
    <div style="background:{ren_color};border-radius:14px;padding:22px 26px;margin-bottom:16px">
    <div style="font-size:2.2em;margin-bottom:8px">{ren_emoji}</div>
    <div style="font-size:1.25em;font-weight:bold;margin-bottom:10px">{ren_titulo}</div>
    <div style="font-size:.95em;line-height:1.6">{ren_texto}</div>
    </div>
    """, unsafe_allow_html=True)

    # ── Por cada pedido entregado ────────────────────────────────────────
    if n_ent > 0:
        aov_v      = pl["aov"]
        cogs_u     = pl["cogs"]      / n_ent
        flete_u    = pl["flete"]     / n_ent
        devol_u    = pl["costo_devol"] / max(n_ent, 1)
        ads_u      = gasto_ads       / max(n_ent, 1)
        ganancia_u = aov_v - cogs_u - flete_u - ads_u - devol_u

        st.markdown("#### 📦 Por cada pedido que llega a tu cliente, así se mueve el dinero:")
        st.caption(f"Promedio de tus {n_ent} pedidos entregados en el período seleccionado")

        _items = [
            ("💵 El cliente paga",        f"${aov_v:,.0f}",    "#e8f4ff",
             "El precio que pone tu tienda"),
            ("📦 Costo producto (Dropi)",  f"−${cogs_u:,.0f}",  "#fee2e2",
             "Lo que te cobra Dropi por el suplemento"),
            ("🚚 Envío (mensajero)",       f"−${flete_u:,.0f}", "#fff3cd",
             "El costo de la paquetería"),
            ("📣 Anuncios (tu parte)",     f"−${ads_u:,.0f}",   "#fce7f3",
             "Costo de publicidad repartido por entrega"),
            ("↩️ Devoluciones (promedio)", f"−${devol_u:,.0f}", "#f3e8ff",
             "Lo que cuesta en promedio por cada devolución"),
            ("✅ Te queda limpio",         f"${ganancia_u:,.0f}",
             "#ccffcc" if ganancia_u > 0 else "#ffcccc",
             "Tu ganancia real por cada pedido entregado"),
        ]
        cols_ord = st.columns(6)
        for col, (lbl, val, color, tip) in zip(cols_ord, _items):
            col.markdown(f"""
            <div style="background:{color};border-radius:10px;padding:12px 10px;
                        text-align:center;min-height:90px">
            <div style="font-size:.75em;opacity:.8">{lbl}</div>
            <div style="font-size:1.3em;font-weight:bold;margin:4px 0">{val}</div>
            <div style="font-size:.68em;opacity:.65">{tip}</div>
            </div>""", unsafe_allow_html=True)

        emoji_r = "🟢" if ganancia_u > 0 else "🔴"
        txt_r   = (
            f"Ganas <b>${ganancia_u:,.0f} MXN por cada pedido que se entrega</b>. "
            "El reto ahora es conseguir más pedidos y que más lleguen al cliente."
            if ganancia_u > 0 else
            f"<b>Pierdes ${abs(ganancia_u):,.0f} MXN por pedido</b>. "
            "El problema principal es el costo de los anuncios. "
            "Necesitas mejorar el ROAS (que las campañas conviertan más barato)."
        )
        st.markdown(f"""
        <div style="background:{'#d4edda' if ganancia_u > 0 else '#ffcccc'};
                    border-radius:8px;padding:12px 16px;margin-top:10px">
        {emoji_r} {txt_r}
        </div>""", unsafe_allow_html=True)

    st.markdown("---")

    # ── BLOQUE 4: ¿Dónde se escapa el dinero? ───────────────────────────
    st.markdown("### 🕳️ ¿Dónde se escapa el dinero?")

    total_costos = pl["cogs"] + pl["flete"] + pl["gasto_mxn"] + pl["costo_devol"]
    if total_costos > 0:
        _costos = [
            ("📣 Anuncios de Facebook/Instagram", pl["gasto_mxn"],
             "Pagas por cada persona que ve tu anuncio. Si no compra, igual pagas. "
             "Es el costo más controlable — si el anuncio convierte bien, sube. Si no, baja.",
             "#8b5cf6"),
            ("📦 Costo del producto (Dropi)",      pl["cogs"],
             "Lo que Dropi te cobra por cada suplemento. Este costo baja si vendes más volumen "
             "porque puedes negociar mejores condiciones.",
             "#ef4444"),
            ("🚚 Envíos (flete)",                  pl["flete"],
             "El mensajero cobra por cada paquete que manda. "
             "Si subes el ticket (vendes 2 unidades en vez de 1), el flete pesa menos.",
             "#f97316"),
            ("↩️ Devoluciones",                    pl["costo_devol"],
             "Cuando alguien regresa el producto, Dropi te cobra el envío de vuelta. "
             "Reducir devoluciones = reducir este costo directamente.",
             "#ec4899"),
        ]
        leak_cols = st.columns(4)
        for col, (nombre, monto, desc, color) in zip(leak_cols, _costos):
            pct = monto / max(total_costos, 1) * 100
            col.markdown(f"""
            <div style="background:#f8f9fa;border-left:4px solid {color};
                        border-radius:8px;padding:14px;min-height:150px">
            <div style="font-size:.85em;font-weight:bold">{nombre}</div>
            <div style="font-size:1.5em;font-weight:bold;color:{color}">${monto:,.0f}</div>
            <div style="font-size:.78em;color:#555">{pct:.0f}% de tus costos totales</div>
            <div style="font-size:.72em;margin-top:6px;opacity:.75;line-height:1.4">{desc}</div>
            </div>""", unsafe_allow_html=True)

        main_leak = max(_costos, key=lambda x: x[1])
        st.markdown(f"""
        <div style="background:#fff3cd;border-radius:10px;padding:14px 18px;margin-top:12px">
        <b>👉 Tu mayor gasto ahora mismo es: {main_leak[0]}</b>
        (${main_leak[1]:,.0f} MXN — {main_leak[1]/max(total_costos,1)*100:.0f}% del total)<br>
        <span style="font-size:.9em">{main_leak[2]}</span>
        </div>""", unsafe_allow_html=True)

        # Comparación vs revenue
        if rev > 0:
            st.markdown("#### 📊 De cada $100 que entran, ¿cómo se reparten?")
            rep_cols = st.columns(5)
            rep_items = [
                ("📦 Producto",      pl["cogs"]       / rev * 100, "#ef4444"),
                ("🚚 Envíos",        pl["flete"]      / rev * 100, "#f97316"),
                ("📣 Anuncios",      pl["gasto_mxn"]  / rev * 100, "#8b5cf6"),
                ("↩️ Devoluciones",  pl["costo_devol"]/ rev * 100, "#ec4899"),
                ("✅ Ganancia",       max(pl["util_pct"], 0),       "#22c55e"),
            ]
            for col_r, (lbl_r, pct_r, color_r) in zip(rep_cols, rep_items):
                col_r.markdown(f"""
                <div style="background:{color_r}22;border:2px solid {color_r};
                            border-radius:10px;padding:12px;text-align:center">
                <div style="font-size:.8em">{lbl_r}</div>
                <div style="font-size:1.6em;font-weight:bold;color:{color_r}">${pct_r:.1f}</div>
                <div style="font-size:.7em;opacity:.7">de cada $100</div>
                </div>""", unsafe_allow_html=True)
    else:
        st.info("Carga datos con ventas para ver el desglose de costos.")

    st.markdown("---")

    # ── BLOQUE 5: ¿Cuándo seré rentable? ────────────────────────────────
    st.markdown("### 📈 ¿Cuándo seré rentable de forma consistente?")

    _sem_recent = load_metricas_semanales().tail(6)
    _gan_por_orden = 600.83  # promedio real de cartera_dropi

    if not _sem_recent.empty and _sem_recent["total_ordenes"].mean() > 0:
        avg_ord_sem   = _sem_recent["total_ordenes"].mean()
        avg_ent_sem   = _sem_recent["entregadas"].mean()
        avg_ads_sem   = _sem_recent["gasto_ads_mxn"].mean()
        delivery_rate = avg_ent_sem / max(avg_ord_sem, 1)

        gan_sem_actual = avg_ent_sem * _gan_por_orden
        util_sem_actual = gan_sem_actual - avg_ads_sem

        be_ent_needed = avg_ads_sem / max(_gan_por_orden, 1)
        be_ord_needed = be_ent_needed / max(delivery_rate, 0.01)

        if util_sem_actual > 0:
            proj_color = "#ccffcc"
            proj_titulo = "🟢 ¡YA ERES RENTABLE en el ritmo actual!"
            proj_texto = (
                f"Con ~{avg_ent_sem:.0f} entregas por semana y ${_gan_por_orden:,.0f} MXN "
                f"de ganancia Dropi por entrega, generas ${gan_sem_actual:,.0f} MXN/sem "
                f"que cubre tus ${avg_ads_sem:,.0f} MXN de anuncios. "
                f"Te sobran <b>${util_sem_actual:,.0f} MXN/semana</b>. "
                f"Ahora el objetivo es escalar sin que el ROAS se caiga."
            )
            proj_cuando = "Ahora mismo"
        else:
            _gap = be_ord_needed - avg_ord_sem
            _semanas_falta = max(int(_gap / max(avg_ord_sem * 0.25, 1)), 1)
            proj_color = "#fff3cc"
            proj_titulo = f"🟡 Necesitas {be_ord_needed:.0f} pedidos/semana para cubrir tus anuncios"
            proj_texto = (
                f"Ahora tienes ~{avg_ord_sem:.0f} pedidos/semana y se entregan ~{avg_ent_sem:.0f}. "
                f"La ganancia de Dropi por esas entregas es ${gan_sem_actual:,.0f} MXN, "
                f"pero gastas ${avg_ads_sem:,.0f} MXN en anuncios. "
                f"Pierdes ${abs(util_sem_actual):,.0f} MXN/semana. "
                f"Necesitas llegar a {be_ord_needed:.0f} pedidos/semana para cubrir los anuncios "
                f"— y subir más para tener ganancia real. "
                f"Si creces 25% cada semana, llegarías en ~{_semanas_falta} semanas."
            )
            proj_cuando = f"~{_semanas_falta} semanas si creces 25%/sem"

        st.markdown(f"""
        <div style="background:{proj_color};border-radius:12px;padding:18px 22px;margin-bottom:14px">
        <div style="font-size:1.15em;font-weight:bold;margin-bottom:8px">{proj_titulo}</div>
        <div style="font-size:.92em;line-height:1.6">{proj_texto}</div>
        </div>""", unsafe_allow_html=True)

        # Escenarios
        st.markdown("#### 🔮 ¿Qué pasaría si escalas?")
        sc_a, sc_b, sc_c, sc_d = st.columns(4)
        _scenarios = [
            ("50 pedidos/sem",  50,  avg_ads_sem * (50 / max(avg_ord_sem, 1))),
            ("100 pedidos/sem", 100, avg_ads_sem * (100 / max(avg_ord_sem, 1))),
            ("200 pedidos/sem", 200, avg_ads_sem * (200 / max(avg_ord_sem, 1))),
            ("500 pedidos/sem", 500, avg_ads_sem * (500 / max(avg_ord_sem, 1))),
        ]
        for col_sc, (lbl_sc, n_sc, ads_sc) in zip([sc_a, sc_b, sc_c, sc_d], _scenarios):
            del_sc   = n_sc * delivery_rate
            gan_sc   = del_sc * _gan_por_orden
            util_sc  = gan_sc - ads_sc
            _is_pos  = util_sc > 0
            col_sc.markdown(f"""
            <div style="background:{'#d4edda' if _is_pos else '#ffcccc'};
                        border-radius:10px;padding:14px;text-align:center">
            <div style="font-weight:bold;font-size:.88em">📦 {lbl_sc}</div>
            <div style="font-size:1.25em;font-weight:bold;margin:6px 0">
            {'🟢 +$' if _is_pos else '🔴 −$'}{abs(util_sc):,.0f}
            </div>
            <div style="font-size:.72em;opacity:.8">
            Utilidad/semana<br>
            {del_sc:.0f} entregas · ${gan_sc:,.0f} Dropi<br>
            −${ads_sc:,.0f} anuncios
            </div>
            </div>""", unsafe_allow_html=True)

        st.caption("⚠️ Escenarios asumen ROAS constante. En la práctica puede bajar al escalar. "
                   "Sube el presupuesto de a 25% cada 3-4 días y monitorea.")

        # Proyección 30/60/90 días
        st.markdown("#### 📆 Proyección a 30, 60 y 90 días")
        st.caption("Si mantienes el ritmo actual de pedidos y gastos")

        _gasto_sem_proj  = avg_ads_sem
        _ords_sem_proj   = avg_ord_sem
        _ent_sem_proj    = avg_ent_sem
        _gan_sem_proj    = _ent_sem_proj * _gan_por_orden
        _util_sem_proj   = _gan_sem_proj - _gasto_sem_proj

        _pj30, _pj60, _pj90 = st.columns(3)
        for _col_pj, _dias_pj, _label_pj in [(_pj30, 30, "30 días"), (_pj60, 60, "60 días"), (_pj90, 90, "90 días")]:
            _sems = _dias_pj / 7
            _rev_pj   = _ent_sem_proj * _sems * pl["aov"] if pl["aov"] > 0 else 0
            _util_pj  = _util_sem_proj * _sems
            _ads_pj   = _gasto_sem_proj * _sems
            _gan_pj   = _gan_sem_proj * _sems
            _is_pos   = _util_pj > 0
            _col_pj.markdown(f"""
            <div style="background:{'#d4edda' if _is_pos else '#ffcccc'};
                        border-radius:12px;padding:16px;text-align:center">
            <div style="font-weight:bold;font-size:1em">📆 {_label_pj}</div>
            <div style="font-size:1.4em;font-weight:900;margin:8px 0;color:{'#16a34a' if _is_pos else '#dc2626'}">
            {'➕' if _is_pos else '➖'}${abs(_util_pj):,.0f}
            </div>
            <div style="font-size:.8em;line-height:1.6;opacity:.85">
            Revenue estimado: ${_rev_pj:,.0f}<br>
            Ganancia Dropi: ${_gan_pj:,.0f}<br>
            Gasto en anuncios: ${_ads_pj:,.0f}<br>
            <b>Utilidad: {'✅' if _is_pos else '❌'} ${_util_pj:+,.0f}</b>
            </div>
            </div>""", unsafe_allow_html=True)
        st.caption("⚠️ Proyección lineal. En la práctica el ROAS cambia al escalar. Úsala de referencia.")

        # Meta del millón
        st.markdown("#### 🎯 ¿Cuánto necesito para llegar a $1,000,000 MXN en un mes?")
        _aov_meta = pl["aov"] if pl["aov"] > 0 else 1150
        _ords_mes = int(1_000_000 / max(_aov_meta, 1))
        _ords_sem = int(_ords_mes / 4.3)
        _ads_mes  = int(avg_ads_sem * 4.3 * (_ords_sem / max(avg_ord_sem, 1)))
        _gan_mes  = _ords_mes * delivery_rate * _gan_por_orden
        _util_mes = _gan_mes - _ads_mes
        _veces    = _ords_sem / max(avg_ord_sem, 1)

        st.markdown(f"""
        <div style="background:#e8f4ff;border-radius:12px;padding:18px 22px">
        <b>Para $1,000,000 MXN en ventas necesitas:</b><br><br>
        📦 <b>~{_ords_mes:,} pedidos al mes</b> ({_ords_sem:,}/semana) — ahora estás en ~{avg_ord_sem:.0f}/semana
        → necesitas <b>{_veces:.0f}x más pedidos</b><br>
        📣 <b>Presupuesto estimado en anuncios:</b> ~${_ads_mes:,.0f} MXN/mes (€{_ads_mes/tasa:,.0f})<br>
        💰 <b>Ganancia Dropi proyectada:</b> ~${_gan_mes:,.0f} MXN/mes<br>
        ✅ <b>Utilidad estimada:</b> ~${_util_mes:,.0f} MXN/mes<br><br>
        <span style="font-size:.85em;opacity:.8">
        Esto asume que el ROAS y la tasa de entrega se mantienen constantes al escalar.
        El camino real: sube pedidos semana a semana y verifica que el ROAS no baje de 2×.
        </span>
        </div>""", unsafe_allow_html=True)
    else:
        st.info("Necesitas más semanas de datos para proyectar. Sigue corriendo campañas.")

    st.markdown("---")

    # ── BLOQUE 6: ¿En qué invertir más tiempo? ──────────────────────────
    st.markdown("### 🎯 ¿En qué invertir más tiempo esta semana?")

    _prios = []
    _roas_d  = pl["roas_real"]
    _mer_d   = pl["mer"]

    if pct_ent < 40:
        _prios.append((1, "🚚 Mejorar la tasa de entregas",
            f"Solo el {pct_ent:.0f}% de tus pedidos llegan al cliente. "
            f"Eso significa que {100-pct_ent:.0f} de cada 100 pedidos te cuestan dinero sin darte ganancia. "
            "Habla con Dropi esta semana: ¿por qué no se entregan? ¿Es el carrier, la dirección, el producto? "
            "→ Cada 10% más de entregas = ~25% más de ganancia sin gastar más en anuncios."))
    if pct_can > 15:
        _prios.append((2, "📞 Llamar a clientes antes de despachar",
            f"El {pct_can:.0f}% de tus pedidos se cancelan. Si vendes 100, pierdes 15+ pedidos. "
            "La solución más fácil: cuando entra un pedido, manda un WhatsApp confirmando la dirección y el horario. "
            "Esto reduce cancelaciones entre 30-40% sin costo extra. "
            "→ Hazlo hoy mismo con los pedidos que tienes en pipeline."))
    if _mer_d < 1.5 and gasto_ads > 0:
        _prios.append((3, "🎬 Crear anuncios nuevos",
            f"Tu MER actual es {_mer_d:.2f}x — significa que los anuncios que tienes ya no están convirtiendo bien. "
            "La gente los ha visto muchas veces y ya no hace click. "
            "Graba 2-3 videos nuevos con un ángulo diferente: ¿qué problema resuelve tu suplemento? "
            "→ Un buen creativo nuevo puede duplicar las ventas con el mismo presupuesto."))
    if _roas_d >= 2.0 and _mer_d >= 2.0:
        _prios.append((1, "💰 Subir el presupuesto de anuncios",
            f"Tu ROAS es {_roas_d:.1f}x — por cada $1 que pones en anuncios, recuperas ${_roas_d:.1f}. "
            "Este es el momento de meter más dinero. "
            "→ Sube el presupuesto diario 20-25% y espera 48h antes de subir más. "
            "No subas más del 30% de golpe o Meta resetea la optimización."))
    if pl["aov"] > 0 and pl["aov"] < 1300:
        _prios.append((4, "📦 Crear bundles (packs de 2 productos)",
            f"Tu ticket promedio es ${pl['aov']:,.0f} MXN. "
            "Si vendes Shilajit + Ashwagandha juntos como pack, el cliente paga más "
            "pero tú sigues pagando el mismo anuncio. "
            "→ Crea un bundle en Dropi a $1,499-$1,799 MXN. "
            "Con el mismo tráfico, tu ROAS sube automáticamente."))
    _prios.append((5, "📊 Revisar el dashboard cada lunes",
        "20 minutos los lunes: ¿subió o bajó el ROAS? ¿Las entregas mejoraron? "
        "¿Qué anuncio funcionó mejor? Las decisiones con datos valen 10x más que las del instinto. "
        "→ Usa el filtro de fecha para comparar esta semana vs la anterior."))

    _prios.sort(key=lambda x: x[0])
    for _i, (_, _titulo, _texto) in enumerate(_prios, 1):
        _bg = "#ffcccc" if _i == 1 and _roas_d < 1.5 else "#fff3cc" if _i <= 2 else "#f0f9ff"
        _border = "#ef4444" if _i == 1 and _roas_d < 1.5 else "#f59e0b" if _i <= 2 else "#3b82f6"
        st.markdown(f"""
        <div style="background:{_bg};border-left:4px solid {_border};border-radius:8px;
                    padding:14px 18px;margin-bottom:8px">
        <div style="font-weight:bold;margin-bottom:4px">{_i}. {_titulo}</div>
        <div style="font-size:.9em;line-height:1.5">{_texto}</div>
        </div>""", unsafe_allow_html=True)

    st.markdown("---")

    # ── BLOQUE 7: Estado de todas tus órdenes ───────────────────────────
    st.markdown("### 📦 ¿Qué está pasando con tus pedidos?")

    if not orders.empty:
        _real_ord = orders[orders["es_prueba"] == 0]
        _status_counts = _real_ord["estatus"].value_counts().reset_index()
        _status_counts.columns = ["Estatus", "Pedidos"]

        _status_simple = {
            "ENTREGADO":                      ("✅ Llegó al cliente",           "#22c55e"),
            "CANCELADO":                      ("❌ Cancelado",                  "#ef4444"),
            "PENDIENTE CONFIRMACION":         ("⏳ Esperando confirmar",         "#f59e0b"),
            "GUIA_GENERADA":                  ("📋 Guía lista, no salió aún",   "#60a5fa"),
            "DEVOLUCION":                     ("↩️ El cliente lo regresó",      "#f97316"),
            "NOVEDAD":                        ("⚠️ Problema en entrega",        "#f97316"),
            "RECEPCION CENTRO DE ENTREGA":    ("🏭 En bodega del mensajero",    "#818cf8"),
            "SALIDA DE CENTRO DE DISTRIBUCION":("🚚 Salió a entregar",          "#34d399"),
            "EN CIUDAD DE DESTINO":           ("🏙️ Llegó a la ciudad",          "#34d399"),
            "EN CIUDAD DE ORIGEN":            ("🔄 Volviendo al origen",        "#f97316"),
            "ASIGNADO A MENSAJERO":           ("🚴 El mensajero lo tiene",      "#34d399"),
            "DEVOLUCION EN PROCESO":          ("↩️ Devolución en proceso",      "#f97316"),
        }

        _rows_stat = []
        for _, row in _status_counts.iterrows():
            _label, _color = _status_simple.get(row["Estatus"],
                                                 (f"🔷 {row['Estatus']}", "#94a3b8"))
            _rev_stat = float(_real_ord[_real_ord["estatus"] == row["Estatus"]]["valor_venta"].sum())
            _rows_stat.append({
                "¿Qué significa?":     _label,
                "Estatus Dropi":       row["Estatus"],
                "Pedidos":             int(row["Pedidos"]),
                "Valor (MXN)":         f"${_rev_stat:,.0f}",
                "_color":              _color,
            })

        _df_stat = pd.DataFrame(_rows_stat)
        _colors_stat = _df_stat["_color"].tolist()
        _df_stat_show = _df_stat.drop(columns="_color").reset_index(drop=True)

        def _hl_stat(row):
            return [f"background-color:{_colors_stat[row.name]}22"] * len(row)

        st.dataframe(_df_stat_show.style.apply(_hl_stat, axis=1),
                     hide_index=True, use_container_width=True)

        # Totales rápidos
        _tot_a, _tot_b, _tot_c, _tot_d = st.columns(4)
        _tot_a.metric("Total pedidos",     n_tot)
        _tot_b.metric("✅ Entregados",     n_ent,      help="Estos generaron ganancia en Dropi")
        _tot_c.metric("⏳ En camino",      n_pip,      help=f"${pl['pipeline_rev']:,.0f} MXN pendientes de cobrar")
        _tot_d.metric("❌ Cancelados",     pl["n_canceladas"])

    st.markdown("---")

    st.markdown("---")

    # ── BLOQUE PIPELINE FINANCIERO ───────────────────────────────────────
    st.markdown("### 🔭 ¿Cuánto dinero me falta por cobrar y cuándo llegará?")
    st.caption("Esto responde si tu negocio es viable aunque hoy gastes más de lo que ingresa.")

    _pipeline_df = load_pipeline_full()
    _delivery_df = load_delivery_times()

    # Apply sidebar filters to pipeline (carrier + product, NOT date — pipeline = current state)
    if sel_carrier:
        _pipeline_df = _pipeline_df[_pipeline_df["transportadora"].isin(sel_carrier)]
    if sel_prod and "tienda" in _pipeline_df.columns:
        _tiendas_pip = {_PROD_TIENDA[p] for p in sel_prod if p in _PROD_TIENDA}
        if _tiendas_pip:
            _pipeline_df = _pipeline_df[_pipeline_df["tienda"].isin(_tiendas_pip)]
    # Filter delivery times by carrier
    if sel_carrier and not _delivery_df.empty:
        _delivery_df = _delivery_df[_delivery_df["transportadora"].isin(sel_carrier)]
    # Add note about no date filter for pipeline
    if sel_carrier or sel_prod:
        st.info(f"🔍 Filtros activos en pipeline: {', '.join(sel_carrier + sel_prod)}")

    if _pipeline_df.empty:
        st.info("No hay órdenes en pipeline actualmente.")
    else:
        _hoy = pd.Timestamp(date.today())
        _tot_pip      = len(_pipeline_df)
        _valor_pip    = float(_pipeline_df["valor_venta"].sum())

        # ── Clasificar por riesgo según edad ─────────────────────────────
        # Entrega real tarda 7-12 días. Ordenes >21 días sin resolver = riesgo alto.
        def _riesgo(dias, estatus):
            if estatus in ("GUIA_GENERADA", "RECEPCION CENTRO DE ENTREGA",
                           "SALIDA DE CENTRO DE DISTRIBUCION", "EN CIUDAD DE DESTINO",
                           "ASIGNADO A MENSAJERO", "LISTO PARA ENTREGAR",
                           "EN CIUDAD DE ORIGEN"):
                if dias <= 14:
                    return "🟢 Llegará pronto", "bajo"
                else:
                    return "🟡 Demorado pero posible", "medio"
            elif estatus == "PENDIENTE CONFIRMACION":
                if dias <= 14:
                    return "🟡 Esperando confirmación", "medio"
                else:
                    return "🔴 Atascada (>14d sin confirmar)", "alto"
            elif estatus == "NOVEDAD":
                return "🔴 Con problemas", "alto"
            elif estatus in ("ENVÍO RECOLECTADO", "EN CAMINO A CIUDAD DE ORIGEN DEVOLUCIÓN",
                             "EN CIUDAD DE DESTINO DEVOLUCIÓN", "PARA DEVOLUCIÓN"):
                return "🟠 Posible devolución", "devolucion"
            else:
                return "🟡 En proceso", "medio"

        _pipeline_df = _pipeline_df.copy()
        _pipeline_df[["riesgo_label", "riesgo_nivel"]] = _pipeline_df.apply(
            lambda r: pd.Series(_riesgo(r["dias_edad"], r["estatus"])), axis=1)

        # Tasas de entrega por nivel (según respuesta del usuario: 70-80% para enviadas)
        _tasa_bajo    = 0.80
        _tasa_medio   = 0.55
        _tasa_alto    = 0.25
        _tasa_devol   = 0.05
        _gan_por_orden = 600.83  # real promedio cartera

        def _gan_esperada(row):
            t = {"bajo": _tasa_bajo, "medio": _tasa_medio,
                 "alto": _tasa_alto, "devolucion": _tasa_devol}[row["riesgo_nivel"]]
            return t * _gan_por_orden

        _pipeline_df["prob_entrega"]  = _pipeline_df["riesgo_nivel"].map(
            {"bajo": _tasa_bajo, "medio": _tasa_medio,
             "alto": _tasa_alto, "devolucion": _tasa_devol})
        _pipeline_df["gan_esperada"]  = _pipeline_df.apply(_gan_esperada, axis=1)
        _pipeline_df["ordenes_esperadas"] = _pipeline_df["prob_entrega"]

        # Resumen por nivel
        _pip_grp = _pipeline_df.groupby("riesgo_nivel").agg(
            ordenes=("orden_id","count"),
            valor=("valor_venta","sum"),
            gan_esp=("gan_esperada","sum"),
        ).reset_index()

        _total_gan_esperada = float(_pipeline_df["gan_esperada"].sum())
        _total_entregas_esp = float((_pipeline_df["prob_entrega"]).sum())

        # ── KPIs del pipeline ─────────────────────────────────────────────
        pk1, pk2, pk3, pk4 = st.columns(4)
        pk1.metric("📦 Órdenes en camino",      f"{_tot_pip}",
                   help="Total de pedidos que aún no tienen resultado final")
        pk2.metric("💵 Valor bruto en tránsito", f"${_valor_pip:,.0f} MXN",
                   help="Lo que pagarían los clientes si todos los pedidos se entregan")
        pk3.metric("💰 Ganancia Dropi esperada", f"${_total_gan_esperada:,.0f} MXN",
                   help="Lo que recibirías de Dropi aplicando la tasa de entrega realista por riesgo")
        _ads_dia = float(_pipeline_df["dias_edad"].count()) * 0  # placeholder
        # Usar gasto semanal promedio de semanas recientes
        _sem_rec2 = load_metricas_semanales().tail(4)
        _gasto_dia_ads = float(_sem_rec2["gasto_ads_mxn"].mean() / 7) if not _sem_rec2.empty else 0
        _dias_cubiertos = int(_total_gan_esperada / max(_gasto_dia_ads, 1))
        pk4.metric("📅 Días de anuncios cubiertos", f"{_dias_cubiertos} días",
                   help=f"Con la ganancia esperada del pipeline cubres {_dias_cubiertos} días de ads "
                        f"(asumiendo ${_gasto_dia_ads:,.0f} MXN/día de gasto actual)")

        # ── Explicación simple ────────────────────────────────────────────
        _color_pip = "#ccffcc" if _dias_cubiertos >= 15 else ("#fff3cc" if _dias_cubiertos >= 7 else "#ffcccc")
        st.markdown(f"""
        <div style="background:{_color_pip};border-radius:10px;padding:16px 20px;margin:12px 0">
        <b>👶 En palabras simples:</b><br><br>
        Tienes <b>{_tot_pip} pedidos</b> que están en camino o esperando resultado.
        Si se entregan con la tasa realista que tú esperas (70-80% los que ya salieron,
        menos los que están atascados), recibirías <b>${_total_gan_esperada:,.0f} MXN</b>
        de ganancia de Dropi.<br><br>
        Con eso podrías pagar <b>{_dias_cubiertos} días de anuncios</b> sin meter más dinero.
        {"✅ <b>Eso te da colchón suficiente para seguir operando.</b>" if _dias_cubiertos >= 15
        else "⚠️ <b>Hay que acelerar las entregas o reducir el gasto de ads temporalmente.</b>"
        if _dias_cubiertos >= 7
        else "🔴 <b>La situación es ajustada. Prioriza recuperar el pipeline atascado.</b>"}
        </div>
        """, unsafe_allow_html=True)

        # ── Desglose por nivel de riesgo ──────────────────────────────────
        st.markdown("#### 🚦 ¿Qué probabilidad tiene cada grupo de pedidos de entregarse?")

        _nivel_info = {
            "bajo":      ("🟢 Llegará pronto",           _tasa_bajo,   "#ccffcc"),
            "medio":     ("🟡 En proceso / demorado",    _tasa_medio,  "#fff3cc"),
            "alto":      ("🔴 Atascado / con problemas", _tasa_alto,   "#ffcccc"),
            "devolucion":("🟠 Va a devolución",          _tasa_devol,  "#fff0e0"),
        }

        _riesgo_rows = []
        for nivel, (label, tasa, color) in _nivel_info.items():
            _sub = _pipeline_df[_pipeline_df["riesgo_nivel"] == nivel]
            if len(_sub) == 0:
                continue
            _riesgo_rows.append({
                "Estado":               label,
                "Pedidos":              len(_sub),
                "Valor bruto":          f"${_sub['valor_venta'].sum():,.0f}",
                "% que entregará":      f"{tasa*100:.0f}%",
                "Ganancia esperada":    f"${_sub['gan_esperada'].sum():,.0f} MXN",
                "Edad prom. (días)":    f"{_sub['dias_edad'].mean():.0f}",
                "_color":               color,
            })

        _df_riesgo = pd.DataFrame(_riesgo_rows)
        if not _df_riesgo.empty:
            _colors_riesgo = _df_riesgo["_color"].tolist()
            _df_riesgo_show = _df_riesgo.drop(columns="_color").reset_index(drop=True)
            st.dataframe(
                _df_riesgo_show.style.apply(
                    lambda row: [f"background-color:{_colors_riesgo[row.name]}"] * len(row), axis=1),
                hide_index=True, use_container_width=True)

        # ── Alerta órdenes atascadas ──────────────────────────────────────
        _atascadas = _pipeline_df[_pipeline_df["riesgo_nivel"] == "alto"]
        if len(_atascadas) > 0:
            _val_atascado = float(_atascadas["valor_venta"].sum())
            st.markdown(f"""
            <div style="background:#fee2e2;border-left:4px solid #ef4444;border-radius:8px;
                        padding:14px 18px;margin-top:8px">
            ⚠️ <b>Tienes {len(_atascadas)} pedidos atascados</b> (${_val_atascado:,.0f} MXN en valor)
            que llevan más de 14 días sin resolverse.<br>
            <b>Acción:</b> Entra a Dropi, filtra por PENDIENTE CONFIRMACION y NOVEDAD,
            y contacta a la transportadora de cada uno. Muchos pueden rescatarse con
            una llamada. Cada entrega recuperada = ${_gan_por_orden:,.0f} MXN para ti.
            </div>
            """, unsafe_allow_html=True)

        # ── Proyección semanal de cuándo entrará el dinero ────────────────
        st.markdown("---")
        st.markdown("#### 📅 ¿Cuándo llegará el dinero del pipeline a tu cartera?")
        st.caption("Basado en que la entrega tarda en promedio 9.6 días desde el pedido")

        if not _delivery_df.empty:
            _dias_mediana_global = float(_delivery_df["dias_entrega"].median())
        else:
            _dias_mediana_global = 9.0

        _pip_activo = _pipeline_df[_pipeline_df["riesgo_nivel"].isin(["bajo", "medio"])].copy()
        _pip_activo["dias_restantes"] = (_dias_mediana_global - _pip_activo["dias_edad"]).clip(lower=0)
        _pip_activo["semana_estimada"] = (_pip_activo["dias_restantes"] / 7).apply(
            lambda x: f"Semana {int(x)+1}" if x > 0 else "Esta semana")

        _pip_activo["fecha_est"] = _hoy + pd.to_timedelta(
            _pip_activo["dias_restantes"], unit="D")
        _pip_activo["semana_label"] = _pip_activo["fecha_est"].dt.to_period("W-SUN").apply(
            lambda p: f"Sem {p.start_time.strftime('%d/%m')}-{p.end_time.strftime('%d/%m')}"
            if not pd.isna(p) else "?")

        _proj_sem = (_pip_activo.groupby("semana_label").agg(
            ordenes=("orden_id", "count"),
            gan_esperada=("gan_esperada", "sum"),
        ).reset_index().sort_values("semana_label"))

        if not _proj_sem.empty and HAS_PLOTLY:
            _proj_total_acum = _proj_sem["gan_esperada"].cumsum()
            import plotly.graph_objects as go
            from plotly.subplots import make_subplots

            _fig_proj = make_subplots(specs=[[{"secondary_y": True}]])
            _fig_proj.add_trace(go.Bar(
                x=_proj_sem["semana_label"], y=_proj_sem["gan_esperada"],
                name="Ganancia esperada esa semana",
                marker_color="#22c55e",
                text=_proj_sem["gan_esperada"].apply(lambda x: f"${x:,.0f}"),
                textposition="outside",
            ), secondary_y=False)
            _fig_proj.add_trace(go.Scatter(
                x=_proj_sem["semana_label"], y=_proj_total_acum,
                name="Acumulado total",
                line=dict(color="#3b82f6", width=2),
                mode="lines+markers+text",
                text=_proj_total_acum.apply(lambda x: f"${x:,.0f}"),
                textposition="top center",
            ), secondary_y=True)

            # Línea de gasto de ads por semana
            _gasto_sem = _gasto_dia_ads * 7
            _fig_proj.add_hline(
                y=_gasto_sem, line_dash="dash", line_color="red",
                annotation_text=f"Gasto ads/sem ${_gasto_sem:,.0f}",
                secondary_y=False)

            _fig_proj.update_layout(
                title="Ganancia Dropi esperada por semana (pedidos en camino)",
                height=320, margin=dict(t=40, b=20),
                showlegend=True, legend=dict(orientation="h"),
                yaxis_title="MXN esta semana",
                yaxis2_title="MXN acumulado",
            )
            st.plotly_chart(_fig_proj, use_container_width=True)

        # ── Tabla detalle pipeline activo ─────────────────────────────────
        with st.expander(f"📋 Ver las {len(_pip_activo)} órdenes activas en detalle"):
            _pip_show = _pip_activo[[
                "orden_id", "fecha", "estatus", "transportadora",
                "valor_venta", "dias_edad", "riesgo_label", "semana_label"
            ]].rename(columns={
                "orden_id":    "Orden ID",
                "fecha":       "Fecha",
                "estatus":     "Estatus Dropi",
                "transportadora": "Carrier",
                "valor_venta": "Valor MXN",
                "dias_edad":   "Días",
                "riesgo_label":"Riesgo",
                "semana_label":"Entrega est.",
            }).sort_values("Días", ascending=False)
            st.dataframe(_pip_show, hide_index=True, use_container_width=True)

    st.markdown("---")

    # ── LISTA DE ÓRDENES URGENTES A RECUPERAR ────────────────────────────
    st.markdown("### 🚨 Órdenes urgentes — llama hoy para recuperar dinero")
    st.caption("Estas son tus órdenes atascadas más valiosas. Cada una que recuperas = $600 MXN de ganancia en Dropi.")

    _urgentes = _pipeline_df[
        (_pipeline_df["riesgo_nivel"].isin(["alto", "medio"])) &
        (_pipeline_df["dias_edad"] > 10)
    ].copy().sort_values("valor_venta", ascending=False).head(25)

    if _urgentes.empty:
        st.success("✅ No hay órdenes urgentes en este momento. ¡Todo en buen estado!")
    else:
        _total_urgente = float(_urgentes["valor_venta"].sum())
        _gan_urgente   = float(_urgentes["gan_esperada"].sum())

        _ua, _ub, _uc = st.columns(3)
        _ua.metric("🚨 Órdenes a revisar",        f"{len(_urgentes)}",
                   help="Pedidos con más de 10 días sin resolverse que aún podrían entregarse")
        _ub.metric("💵 Valor en riesgo",            f"${_total_urgente:,.0f} MXN",
                   help="Valor bruto de esos pedidos — lo que pagarían los clientes si entregan")
        _uc.metric("💰 Ganancia rescatable",        f"${_gan_urgente:,.0f} MXN",
                   help="Ganancia estimada de Dropi si logras que se entreguen")

        st.markdown(f"""
        <div style="background:#fff3cd;border-left:4px solid #f59e0b;border-radius:8px;
                    padding:14px 18px;margin:8px 0">
        <b>📞 Qué hacer ahora mismo:</b><br>
        1. Entra a Dropi → Pedidos → filtra por PENDIENTE CONFIRMACION y NOVEDAD<br>
        2. Llama a la transportadora con el número de guía de cada pedido de abajo<br>
        3. Pide que re-intenten la entrega o que ubiquen el paquete<br>
        4. Cada pedido que rescatas = <b>${600:,.0f} MXN</b> que entra a tu cartera
        </div>
        """, unsafe_allow_html=True)

        _urg_show = _urgentes[[
            "orden_id", "fecha", "estatus", "transportadora",
            "valor_venta", "dias_edad", "riesgo_label", "numero_guia"
        ]].rename(columns={
            "orden_id":       "Orden ID",
            "fecha":          "Fecha pedido",
            "estatus":        "Estado actual",
            "transportadora": "Carrier",
            "valor_venta":    "Valor MXN",
            "dias_edad":      "Días sin resolver",
            "riesgo_label":   "Riesgo",
            "numero_guia":    "Nº Guía",
        }).reset_index(drop=True)  # reset so row.name is 0,1,2,… matching _urg_colors

        _urg_colors = _urgentes["riesgo_nivel"].map(
            {"alto": "#ffcccc", "medio": "#fff3cc"}).tolist()

        st.dataframe(
            _urg_show.style.apply(
                lambda row: [f"background-color:{_urg_colors[row.name]}"] * len(row), axis=1),
            hide_index=True, use_container_width=True)

        st.caption(f"Mostrando top {len(_urgentes)} por valor. Total en cartera si recuperas el 75%: "
                   f"${_gan_urgente * 0.75:,.0f} MXN")

    st.markdown("---")

    # ── TIEMPOS REALES DE ENTREGA POR TRANSPORTADORA ─────────────────────
    st.markdown("### ⏱️ ¿Cuánto tarda cada transportadora en entregar?")
    st.caption("Calculado con la fecha real de cobro de ganancia en Dropi vs fecha del pedido")

    # Reutiliza _delivery_df ya filtrado por carrier (definido arriba en la sección pipeline)
    _dt_df = _delivery_df
    if _dt_df.empty:
        st.info("Necesitas más órdenes entregadas para calcular tiempos.")
    else:
        _carrier_times = _dt_df.groupby("transportadora").agg(
            n=("dias_entrega", "count"),
            dias_prom=("dias_entrega", "mean"),
            dias_mediana=("dias_entrega", "median"),
            dias_min=("dias_entrega", "min"),
            dias_max=("dias_entrega", "max"),
            pct25=("dias_entrega", lambda x: x.quantile(0.25)),
            pct75=("dias_entrega", lambda x: x.quantile(0.75)),
        ).round(1).reset_index().sort_values("n", ascending=False)

        # Semáforo de velocidad
        def _vel_color(dias):
            return "#ccffcc" if dias <= 8 else ("#fff3cc" if dias <= 12 else "#ffcccc")
        def _vel_label(dias):
            return "🟢 Rápida" if dias <= 8 else ("🟡 Normal" if dias <= 12 else "🔴 Lenta")

        if HAS_PLOTLY:
            _tc1, _tc2 = st.columns(2)
            with _tc1:
                _colors_carr = [_vel_color(d) for d in _carrier_times["dias_prom"]]
                _fig_carr = go.Figure(go.Bar(
                    x=_carrier_times["transportadora"],
                    y=_carrier_times["dias_prom"],
                    marker_color=[c.replace("cc","88") for c in _colors_carr],
                    text=_carrier_times["dias_prom"].apply(lambda x: f"{x:.1f}d"),
                    textposition="outside",
                    error_y=dict(
                        type="data",
                        symmetric=False,
                        array=(_carrier_times["pct75"] - _carrier_times["dias_prom"]).tolist(),
                        arrayminus=(_carrier_times["dias_prom"] - _carrier_times["pct25"]).tolist(),
                        visible=True
                    )
                ))
                _fig_carr.add_hline(y=9.6, line_dash="dash", line_color="#888",
                                    annotation_text="Prom. global 9.6d")
                _fig_carr.update_layout(
                    title="Días promedio hasta entrega por carrier",
                    height=300, margin=dict(t=40, b=15),
                    yaxis_title="Días",
                )
                st.plotly_chart(_fig_carr, use_container_width=True)

            with _tc2:
                _fig_box = go.Figure()
                for _, row_c in _carrier_times.iterrows():
                    _raw = _dt_df[_dt_df["transportadora"] == row_c["transportadora"]]["dias_entrega"]
                    _fig_box.add_trace(go.Box(
                        y=_raw, name=row_c["transportadora"],
                        boxmean=True,
                    ))
                _fig_box.update_layout(
                    title="Distribución de días de entrega",
                    height=300, margin=dict(t=40, b=15),
                    yaxis_title="Días",
                    showlegend=False,
                )
                st.plotly_chart(_fig_box, use_container_width=True)

        # Tabla resumen
        _ct_show = _carrier_times.copy()
        _ct_show["Velocidad"]   = _ct_show["dias_prom"].apply(_vel_label)
        _ct_show["Típico"]      = _ct_show.apply(
            lambda r: f"{r['pct25']:.0f}–{r['pct75']:.0f} días", axis=1)
        _ct_show["dias_prom"]   = _ct_show["dias_prom"].apply(lambda x: f"{x:.1f} días")
        _ct_show["dias_mediana"]= _ct_show["dias_mediana"].apply(lambda x: f"{x:.0f} días")

        _colors_ct = [_vel_color(d) for d in
                      _carrier_times["dias_prom"].tolist()]

        _ct_display = _ct_show[[
            "transportadora","n","Velocidad","dias_prom","dias_mediana","Típico","dias_min","dias_max"
        ]].rename(columns={
            "transportadora":"Carrier","n":"Entregas analizadas",
            "dias_prom":"Promedio","dias_mediana":"Mediana",
            "dias_min":"Mínimo","dias_max":"Máximo",
        }).reset_index(drop=True)

        st.dataframe(
            _ct_display.style.apply(
                lambda row: [f"background-color:{_colors_ct[row.name]}55"] * len(row), axis=1),
            hide_index=True, use_container_width=True)

        st.markdown(f"""
        <div style="background:#e8f4ff;border-radius:10px;padding:14px 18px;margin-top:8px">
        <b>👶 ¿Qué significa esto?</b><br><br>
        La transportadora más rápida es <b>{_carrier_times.sort_values('dias_prom').iloc[0]['transportadora']}</b>
        con {_carrier_times.sort_values('dias_prom').iloc[0]['dias_prom']:.1f} días en promedio.
        La más lenta es <b>{_carrier_times.sort_values('dias_prom').iloc[-1]['transportadora']}</b>
        con {_carrier_times.sort_values('dias_prom').iloc[-1]['dias_prom']:.1f} días.
        El rango normal es de <b>3 a 15 días</b>. Si una orden lleva más de 15 días
        sin resolverse, probablemente está atascada y hay que llamar a la transportadora.
        </div>
        """, unsafe_allow_html=True)

    st.markdown("---")

    # ── FLUJO DE CAJA DÍA A DÍA ──────────────────────────────────────────
    st.markdown("### 💸 Flujo de caja real — ¿cuándo entró y salió el dinero?")
    st.caption("Cada barra es un día. Verde = dinero que entró (ganancias). Rojo = dinero que salió (fletes, retiros).")

    if not cartera_df.empty and HAS_PLOTLY:
        _caj = cartera_df.copy()
        _caj["_fdt"] = pd.to_datetime(_caj["fecha"].str[:10], dayfirst=True, errors="coerce")
        _caj = _caj.dropna(subset=["_fdt"])
        _caj["_signed"] = _caj.apply(
            lambda r: r["monto"] if r["tipo"] == "ENTRADA" else -r["monto"], axis=1)

        _daily = _caj.groupby("_fdt").agg(
            entradas=("_signed", lambda x: x[x > 0].sum()),
            salidas=("_signed",  lambda x: x[x < 0].sum()),
            neto=("_signed", "sum"),
        ).reset_index().sort_values("_fdt")

        # Filter by date if date filter is active (not min/max)
        _caj_start = pd.Timestamp(d_start)
        _caj_end   = pd.Timestamp(d_end)
        _daily_filt = _daily[(_daily["_fdt"] >= _caj_start) & (_daily["_fdt"] <= _caj_end)]

        if not _daily_filt.empty:
            _fig_caja = go.Figure()
            _fig_caja.add_bar(
                x=_daily_filt["_fdt"], y=_daily_filt["entradas"],
                name="Entradas (ganancias)", marker_color="#22c55e",
                text=_daily_filt["entradas"].apply(lambda x: f"${x:,.0f}" if x > 0 else ""),
                textposition="outside",
            )
            _fig_caja.add_bar(
                x=_daily_filt["_fdt"], y=_daily_filt["salidas"],
                name="Salidas (fletes, retiros)", marker_color="#ef4444",
                text=_daily_filt["salidas"].apply(lambda x: f"${x:,.0f}" if x < 0 else ""),
                textposition="outside",
            )
            _fig_caja.add_scatter(
                x=_daily_filt["_fdt"], y=_daily_filt["neto"].cumsum(),
                name="Saldo acumulado neto", line=dict(color="#3b82f6", width=2),
                mode="lines", yaxis="y2",
            )
            _fig_caja.update_layout(
                barmode="relative",
                title="Entradas y salidas diarias en Dropi",
                height=350, margin=dict(t=40, b=20),
                showlegend=True, legend=dict(orientation="h"),
                yaxis=dict(title="MXN día"),
                yaxis2=dict(title="Acumulado MXN", overlaying="y", side="right"),
                xaxis=dict(title="Fecha"),
            )
            st.plotly_chart(_fig_caja, use_container_width=True)

            # KPIs flujo de caja
            _fk1, _fk2, _fk3, _fk4 = st.columns(4)
            _fk1.metric("📥 Total entradas",
                        f"${_daily_filt['entradas'].sum():,.0f} MXN",
                        help="Todo el dinero que Dropi te acreditó en el período")
            _fk2.metric("📤 Total salidas",
                        f"${abs(_daily_filt['salidas'].sum()):,.0f} MXN",
                        help="Fletes iniciales + retiros + devoluciones cobradas")
            _fk3.metric("⚖️ Flujo neto",
                        f"${_daily_filt['neto'].sum():,.0f} MXN",
                        delta="positivo" if _daily_filt['neto'].sum() > 0 else "negativo",
                        delta_color="normal" if _daily_filt['neto'].sum() > 0 else "inverse",
                        help="Entradas menos salidas. Si es positivo, Dropi te generó más de lo que te cobró.")
            _dias_con_entrada = int((_daily_filt["entradas"] > 0).sum())
            _fk4.metric("📅 Días con cobros",
                        f"{_dias_con_entrada} días",
                        help="Días en los que recibiste dinero de Dropi. "
                             "Si es muy bajo, hay pocas entregas o están muy agrupadas.")
        else:
            st.info("No hay movimientos de cartera en el período seleccionado.")
    elif cartera_df.empty:
        st.info("Carga tu cartera de Dropi para ver el flujo de caja.")

    st.markdown("---")

    # ── BLOQUE 8: Movimientos en la cartera Dropi ────────────────────────
    st.markdown("### 📋 Todos tus movimientos en la cartera de Dropi")
    st.caption("Cada fila es una entrada o salida de dinero en tu cuenta de Dropi")

    if cartera_df.empty:
        st.info("Carga tu historial de cartera Dropi desde el sidebar para ver los movimientos.")
    else:
        with st.expander("📖 ¿Qué significa cada tipo de movimiento?"):
            st.markdown("""
| Tipo | ¿Qué es en palabras simples? |
|---|---|
| ✅ **ganancia** | Dropi te acredita el dinero porque un cliente recibió tu producto |
| 📦 **flete_inicial** | Pagas el envío cuando generas la guía (aunque el pedido no llegue) |
| 💜 **retiro** | Sacaste dinero de Dropi a tu banco personal |
| ↩️ **devolucion** | El cliente regresó el producto y Dropi te cobró el envío de vuelta |
| 🔄 **transferencia** | Dinero transferido dentro de Dropi |
| ⚙️ **admin** | Ajuste administrativo de Dropi |
""")

        _tipo_labels = {
            "ganancia":      "✅ Ganancia de entrega",
            "flete_inicial": "📦 Pago de flete",
            "retiro":        "💜 Retiro a banco",
            "devolucion":    "↩️ Costo devolución",
            "transferencia": "🔄 Transferencia",
            "admin":         "⚙️ Ajuste admin",
        }

        _dropi_f_col1, _dropi_f_col2 = st.columns([1, 3])
        _dropi_tipo_f = _dropi_f_col1.selectbox(
            "Filtrar por tipo",
            ["Todos"] + sorted(cartera_df["subtipo"].dropna().unique().tolist()),
            key="dropi_tipo_filter_main")
        _dropi_buscar = _dropi_f_col2.text_input("Buscar (orden ID, descripción…)", key="dropi_buscar_main")

        _df_dropi = cartera_df.copy()
        # Apply date filter from sidebar (using same date range as the rest of the dashboard)
        _df_dropi["_fdt_c"] = pd.to_datetime(_df_dropi["fecha"].str[:10], dayfirst=True, errors="coerce")
        _df_dropi = _df_dropi[
            (_df_dropi["_fdt_c"] >= pd.Timestamp(d_start)) &
            (_df_dropi["_fdt_c"] <= pd.Timestamp(d_end))
        ]
        if _dropi_tipo_f != "Todos":
            _df_dropi = _df_dropi[_df_dropi["subtipo"] == _dropi_tipo_f]
        if _dropi_buscar:
            _df_dropi = _df_dropi[
                _df_dropi["descripcion"].str.contains(_dropi_buscar, case=False, na=False) |
                _df_dropi["orden_id"].astype(str).str.contains(_dropi_buscar, case=False, na=False)
            ]

        _df_dropi["Tipo de movimiento"] = _df_dropi["subtipo"].map(_tipo_labels).fillna(_df_dropi["subtipo"])
        _df_dropi["Monto"] = _df_dropi.apply(
            lambda r: f"{'📥 +' if r['tipo']=='ENTRADA' else '📤 −'}${r['monto']:,.2f}", axis=1)

        st.dataframe(_df_dropi[[
            "fecha", "Tipo de movimiento", "Monto", "monto_previo", "orden_id", "descripcion"
        ]].rename(columns={
            "fecha":          "📅 Fecha",
            "monto_previo":   "Saldo antes (MXN)",
            "orden_id":       "Orden ID",
            "descripcion":    "Descripción",
        }), hide_index=True, use_container_width=True)
        st.caption(f"Mostrando {len(_df_dropi)} de {len(cartera_df)} movimientos")

        # Resumen por tipo (usando los mismos datos filtrados por fecha y tipo)
        _df_dropi_all_tipos = cartera_df.copy()
        _df_dropi_all_tipos["_fdt_c"] = pd.to_datetime(
            _df_dropi_all_tipos["fecha"].str[:10], dayfirst=True, errors="coerce")
        _df_dropi_all_tipos = _df_dropi_all_tipos[
            (_df_dropi_all_tipos["_fdt_c"] >= pd.Timestamp(d_start)) &
            (_df_dropi_all_tipos["_fdt_c"] <= pd.Timestamp(d_end))
        ]
        st.markdown("#### 💡 ¿Cuánto entra y cuánto sale en total?")
        _sum_tipo = (_df_dropi_all_tipos.groupby(["tipo", "subtipo"])
                     .agg(total=("monto", "sum"), movimientos=("monto", "count"))
                     .reset_index())
        _sum_tipo["Tipo"] = _sum_tipo["subtipo"].map(_tipo_labels).fillna(_sum_tipo["subtipo"])
        _sum_tipo["Signo"] = _sum_tipo["tipo"].map({"ENTRADA": "📥 Entra", "SALIDA": "📤 Sale"})
        _sum_tipo["Total MXN"] = _sum_tipo["total"].apply(lambda x: f"${x:,.2f}")
        _sum_tipo_color = _sum_tipo["tipo"].map({"ENTRADA": "#d4edda", "SALIDA": "#ffcccc"})

        def _hl_sum(row):
            return [f"background-color:{_sum_tipo_color.iloc[row.name]}"] * len(row)

        _sum_show = _sum_tipo[["Signo", "Tipo", "movimientos", "Total MXN"]].rename(
            columns={"movimientos": "Movimientos"}).reset_index(drop=True)
        st.dataframe(_sum_show.style.apply(_hl_sum, axis=1),
                     hide_index=True, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════
# TAB 1 — RESUMEN EJECUTIVO
# ═══════════════════════════════════════════════════════════════════════════
with tab_resumen:
    st.markdown("## 📊 Resumen ejecutivo")
    st.caption("Esta pestaña te da el panorama completo de tu negocio de un vistazo. "
               "Es como el tablero de un avión: si todo va bien, no necesitas entrar a los detalles.")
    st.markdown("""
<div style="background:#eff6ff;border-left:4px solid #3b82f6;border-radius:8px;padding:14px 18px;margin-bottom:16px">
<b>👀 ¿Qué mirar primero?</b><br>
• <b>Barras azules</b> = lo que vendiste cada semana. Barras verdes = ganancia real.<br>
• <b>Funnel derecho</b> = de cada 100 pedidos, cuántos llegan al cliente.<br>
• <b>MER y ROAS</b> = qué tan bien trabajan tus anuncios. Más de 2× es bueno.
</div>""", unsafe_allow_html=True)
    if orders.empty:
        st.info("Sin órdenes para el período seleccionado.")
    else:
        # KPI secundarios
        col1, col2, col3, col4, col5, col6 = st.columns(6)
        col1.metric("📦 Órdenes reales", pl["n_total"],
                    _delta(pl["n_total"], pl_prev["n_total"]),
                    help="Cuántas personas compraron. El dinero llega solo cuando el cliente RECIBE el paquete — hasta entonces es solo una promesa.")
        col2.metric("✅ Entregadas",     pl["n_entregadas"],
                    _delta(pl["n_entregadas"], pl_prev["n_entregadas"]),
                    help="Pedidos donde el cliente SÍ recibió el producto. Estos son los únicos que te dan ganancia real en Dropi.")
        col3.metric("⏳ Pipeline real",   pl["n_pipeline"],
                    help=f"Pedidos en camino que aún no han llegado. Cuando lleguen, Dropi te acreditará el dinero. ${pl['pipeline_rev']:,.0f} MXN por cobrar (excluye perdidas).")
        col4.metric("⚰️ Perdidas",       pl.get("n_perdidas", 0),
                    help=f"Órdenes que llevan más de {DIAS_PEDIDO_PERDIDO} días sin resolverse. Probablemente ya no van a llegar — es dinero perdido. ${pl.get('perdidas_rev',0):,.0f} MXN.",
                    delta_color="off")
        col5.metric("↩️ Devoluciones",   pl["n_devol"],
                    help="Cuando el cliente rechazó o regresó el paquete. Te cuesta el flete de ida y de vuelta sin recibir ganancia.")
        col6.metric("📣 Gasto Ads",      f"€{pl['gasto_eur']:.0f}",
                    help="Lo que pagaste en Facebook/Instagram para conseguir clientes en el período seleccionado.")

        if HAS_PLOTLY:
            st.divider()
            r1c1, r1c2 = st.columns([2, 1])

            # Revenue semanal
            with r1c1:
                sem_filt = semanas_df[
                    (semanas_df["fecha_dt"] >= pd.Timestamp(d_start)) &
                    (semanas_df["fecha_dt"] <= pd.Timestamp(d_end))
                ]
                if not sem_filt.empty:
                    st.caption("📈 **Revenue vs Utilidad por semana** — Las barras azules son lo que vendiste (entregas cobradas). "
                               "Las verdes son lo que te quedó después de pagar TODO. Si hay barras rojas, esa semana perdiste dinero.")
                    colors_u = ["#ef4444" if v < 0 else "#22c55e"
                                for v in sem_filt["utilidad_neta"]]
                    fig = go.Figure()
                    fig.add_bar(x=sem_filt["fecha_reporte"], y=sem_filt["ventas_confirmadas"],
                                name="Revenue", marker_color="#60a5fa")
                    fig.add_bar(x=sem_filt["fecha_reporte"], y=sem_filt["utilidad_neta"],
                                name="Utilidad", marker_color=colors_u)
                    fig.update_layout(title="Revenue vs Utilidad por semana",
                                      barmode="overlay", height=280,
                                      margin=dict(t=35, b=15), showlegend=True,
                                      legend=dict(orientation="h"))
                    st.plotly_chart(fig, use_container_width=True)

            # Funnel de órdenes (incluye PERDIDAS)
            with r1c2:
                st.caption("🔽 **Funnel de órdenes** — De cada pedido que entra, ¿qué le pasa? "
                           "Quieres que la barra verde (Entregadas) sea lo más grande posible.")
                funnel_vals   = [pl["n_total"], pl["n_entregadas"], pl["n_pipeline"],
                                 pl.get("n_perdidas", 0), pl["n_devol"], pl["n_canceladas"]]
                funnel_labels = ["Total", "Entregadas", "Pipeline activo",
                                 f"Perdidas (>{DIAS_PEDIDO_PERDIDO}d)", "Dev.", "Canceladas"]
                funnel_colors = ["#60a5fa", "#22c55e", "#f59e0b", "#6b7280", "#f97316", "#ef4444"]
                fig2 = go.Figure(go.Bar(
                    x=funnel_vals, y=funnel_labels, orientation="h",
                    marker_color=funnel_colors,
                    text=[f"{v}" for v in funnel_vals], textposition="inside",
                ))
                fig2.update_layout(title="Funnel de órdenes", height=300,
                                   margin=dict(t=35, b=15), xaxis_title="Órdenes")
                st.plotly_chart(fig2, use_container_width=True)

            st.divider()
            r2c1, r2c2, r2c3 = st.columns(3)

            # MER & ROAS trend
            with r2c1:
                if not sem_filt.empty:
                    st.caption("📉 **MER y ROAS semanal** — Ambas líneas deben estar por encima de 2×. "
                               "Si bajan de la línea roja punteada, estás perdiendo eficiencia en los anuncios.")
                    fig3 = go.Figure()
                    fig3.add_scatter(x=sem_filt["fecha_reporte"], y=sem_filt["mer"],
                                     name="MER", line=dict(color="#8b5cf6", width=2),
                                     mode="lines+markers")
                    fig3.add_scatter(x=sem_filt["fecha_reporte"], y=sem_filt["roas_real"],
                                     name="ROAS Real", line=dict(color="#f97316", width=2),
                                     mode="lines+markers")
                    fig3.add_hline(y=2, line_dash="dash", line_color="red", annotation_text="MER mín")
                    fig3.update_layout(title="MER y ROAS Real", height=230,
                                       margin=dict(t=35, b=15), showlegend=True,
                                       legend=dict(orientation="h"))
                    st.plotly_chart(fig3, use_container_width=True)

            # Distribución de costos (donut)
            with r2c2:
                st.caption("🍩 **¿A dónde se va el dinero?** — Cada trozo del pastel es un costo. "
                           "La tajada verde es tu ganancia. Cuanto más grande sea la verde, mejor.")
                pie_labels = ["COGS", "Flete", "Meta Ads", "Dev.", "Utilidad"]
                pie_vals   = [max(pl["cogs"], 0), max(pl["flete"], 0), max(pl["gasto_mxn"], 0),
                              max(pl["costo_devol"], 0), max(pl["utilidad"], 0)]
                pie_colors = ["#ef4444", "#f97316", "#8b5cf6", "#ec4899", "#22c55e"]
                fig4 = go.Figure(go.Pie(
                    labels=pie_labels, values=pie_vals,
                    marker_colors=pie_colors, hole=0.45, textinfo="label+percent",
                ))
                fig4.update_layout(title="Distribución de costos", height=230,
                                   margin=dict(t=35, b=0), showlegend=False)
                st.plotly_chart(fig4, use_container_width=True)

            # Top ciudades
            with r2c3:
                st.caption("🏙️ **Top ciudades** — ¿Dónde viven tus mejores clientes? "
                           "Sirve para enfocar los anuncios en las zonas que más compran.")
                ent_orders = orders[orders["estatus"] == "ENTREGADO"]
                if not ent_orders.empty and "ciudad_destino" in ent_orders.columns:
                    top_cities = (ent_orders.groupby("ciudad_destino")["valor_venta"]
                                  .sum().nlargest(8).reset_index())
                    top_cities.columns = ["Ciudad", "Revenue"]
                    fig5 = go.Figure(go.Bar(
                        x=top_cities["Revenue"], y=top_cities["Ciudad"],
                        orientation="h", marker_color="#06b6d4",
                        text=top_cities["Revenue"].apply(lambda x: f"${x:,.0f}"),
                        textposition="inside",
                    ))
                    fig5.update_layout(title="Top ciudades por revenue", height=230,
                                       margin=dict(t=35, b=15))
                    st.plotly_chart(fig5, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════
# TAB 2 — FINANCIERO
# ═══════════════════════════════════════════════════════════════════════════
with tab_fin:
    st.markdown("## 💰 Financiero")
    st.caption("Aquí ves el estado de resultados completo: cuánto entraste, cuánto gastaste y qué te quedó. "
               "Es como tu recibo de caja al final del mes.")
    fc1, fc2 = st.columns([1.3, 1])

    with fc1:
        st.subheader("Estado de Resultados")
        st.caption("📋 **Estado de Resultados** — Cada fila es un paso: empieza con lo que vendiste y van restando los costos. "
                   "El número final es lo que realmente ganaste (o perdiste).")
        pl_rows = [
            ("💰 Revenue confirmado",   pl["revenue"],      False),
            ("  − COGS (producto)",    -pl["cogs"],         False),
            ("  − Flete entregadas",   -pl["flete"],        False),
            ("= Margen Bruto",          pl["margen_bruto"], True),
            ("  − Costo devoluciones", -pl["costo_devol"],  False),
            ("  − Meta Ads (MXN)",     -pl["gasto_mxn"],   False),
            ("= Utilidad Neta",         pl["utilidad"],     True),
        ]
        pl_data = pd.DataFrame(pl_rows, columns=["Concepto", "MXN", "_bold"])
        pl_data["MXN"] = pl_data["MXN"].apply(lambda x: f"${x:,.2f}")
        pl_data["Pct"] = ["100%",
                          f"-{pl['cogs']/max(pl['revenue'],1)*100:.1f}%",
                          f"-{pl['flete']/max(pl['revenue'],1)*100:.1f}%",
                          f"{pl['mb_pct']:.1f}%",
                          f"-{pl['costo_devol']/max(pl['revenue'],1)*100:.1f}%",
                          f"-{pl['gasto_mxn']/max(pl['revenue'],1)*100:.1f}%",
                          f"{pl['util_pct']:.1f}%"]
        st.dataframe(pl_data.drop(columns="_bold"), hide_index=True, use_container_width=True)
        st.caption(f"Pipeline no cobrado: ${pl['pipeline_rev']:,.0f} MXN | "
                   f"Gasto Ads: €{pl['gasto_eur']:.2f} × {tasa} = ${pl['gasto_mxn']:,.0f}")

    with fc2:
        if HAS_PLOTLY:
            st.caption("🌊 **Cascada P&L** — Cada bloque rojo reduce tu dinero. El bloque azul final es tu utilidad. "
                       "Si ese bloque está arriba de cero, ganaste.")
            # Waterfall P&L
            wf_labels = ["Revenue", "−COGS", "−Flete", "Margen\nBruto",
                         "−Devol.", "−Ads", "Utilidad"]
            wf_vals   = [pl["revenue"], -pl["cogs"], -pl["flete"], None,
                         -pl["costo_devol"], -pl["gasto_mxn"], None]
            wf_meas   = ["absolute", "relative", "relative", "subtotal",
                         "relative", "relative", "total"]
            fig_wf = go.Figure(go.Waterfall(
                x=wf_labels, y=wf_vals, measure=wf_meas,
                connector=dict(line=dict(color="#94a3b8")),
                increasing=dict(marker_color="#22c55e"),
                decreasing=dict(marker_color="#ef4444"),
                totals=dict(marker_color="#3b82f6"),
            ))
            fig_wf.update_layout(title="Cascada P&L", height=320,
                                 margin=dict(t=35, b=15), showlegend=False)
            st.plotly_chart(fig_wf, use_container_width=True)

    st.divider()

    # Tendencia semanal de KPIs financieros
    if HAS_PLOTLY and not semanas_df.empty:
        period_opts = ["Semanal", "Mensual"]
        period_sel  = st.radio("Granularidad", period_opts, horizontal=True)

        hist = semanas_df[
            (semanas_df["fecha_dt"] >= pd.Timestamp(d_start)) &
            (semanas_df["fecha_dt"] <= pd.Timestamp(d_end))
        ].copy()

        if period_sel == "Mensual":
            hist["periodo"] = hist["fecha_dt"].dt.to_period("M").astype(str)
            hist = hist.groupby("periodo").agg(
                ventas_confirmadas=("ventas_confirmadas", "sum"),
                utilidad_neta=("utilidad_neta", "sum"),
                gasto_ads_mxn=("gasto_ads_mxn", "sum"),
                mer=("mer", "mean"),
                cpa_real=("cpa_real", "mean"),
            ).reset_index().rename(columns={"periodo": "fecha_reporte"})
        else:
            hist["periodo"] = hist["fecha_reporte"]

        if not hist.empty:
            tab_fin_h1, tab_fin_h2 = st.columns(2)
            with tab_fin_h1:
                st.caption("📊 **Revenue / Gasto / Utilidad** — Las barras azules son ventas, las naranjas son lo que gastaste en anuncios "
                           "y las verdes/rojas son tu ganancia neta. Quieres ver crecer el azul sin que el naranja lo supere.")
                fig_rev = go.Figure()
                fig_rev.add_bar(x=hist["fecha_reporte"], y=hist["ventas_confirmadas"],
                                name="Revenue", marker_color="#60a5fa")
                fig_rev.add_bar(x=hist["fecha_reporte"], y=hist["gasto_ads_mxn"],
                                name="Gasto Ads", marker_color="#f97316")
                colors_u2 = ["#ef4444" if v < 0 else "#22c55e" for v in hist["utilidad_neta"]]
                fig_rev.add_bar(x=hist["fecha_reporte"], y=hist["utilidad_neta"],
                                name="Utilidad", marker_color=colors_u2)
                fig_rev.update_layout(title="Revenue / Gasto / Utilidad",
                                      barmode="group", height=300,
                                      margin=dict(t=35, b=15), showlegend=True)
                st.plotly_chart(fig_rev, use_container_width=True)
            with tab_fin_h2:
                st.caption("📉 **MER y CPA** — MER (izquierda) mide cuánto revenue genera cada peso en anuncios — quieres >2×. "
                           "CPA (derecha) es cuánto te cuesta cada venta — quieres que baje.")
                fig_mer = make_subplots(specs=[[{"secondary_y": True}]])
                fig_mer.add_trace(go.Scatter(x=hist["fecha_reporte"], y=hist["mer"],
                    name="MER", line=dict(color="#8b5cf6", width=2), mode="lines+markers"),
                    secondary_y=False)
                fig_mer.add_trace(go.Scatter(x=hist["fecha_reporte"], y=hist["cpa_real"],
                    name="CPA MXN", line=dict(color="#ef4444", width=2, dash="dot"),
                    mode="lines+markers"), secondary_y=True)
                fig_mer.add_hline(y=2.0, line_dash="dash", line_color="red")
                fig_mer.update_layout(title="MER (izq.) y CPA (der.)", height=300,
                                      margin=dict(t=35, b=15), showlegend=True)
                st.plotly_chart(fig_mer, use_container_width=True)

    st.divider()

    # Break-even interactivo
    with st.expander("📐 Calculadora Break-even"):
        be1, be2, be3, be4 = st.columns(4)
        be_precio = be1.number_input("Precio venta (MXN)", value=float(round(pl["aov"], 0)), step=50.0, format="%.0f")
        be_cogs   = be2.number_input("Costo producto (MXN)", value=float(round(pl["cogs"] / max(pl["n_entregadas"],1), 0)), step=10.0, format="%.0f")
        be_flete  = be3.number_input("Flete (MXN)", value=float(round(pl["flete"] / max(pl["n_entregadas"],1), 0)), step=5.0, format="%.0f")
        be_pdev   = be4.number_input("% Devoluciones", value=float(round(pl["n_devol"] / max(pl["n_total"],1)*100, 1)), step=0.5, format="%.1f", min_value=0.0, max_value=100.0)
        be_cdev   = st.number_input("Costo retorno por devolución (MXN)", value=80.0, step=5.0)
        be_cpa    = (be_precio * (1 - be_pdev/100) - be_cogs - be_flete - be_pdev/100 * be_cdev)
        be_col    = "#ccffcc" if pl["cpa_real"] <= be_cpa else "#ffcccc"
        st.markdown(f'<div style="background:{be_col};padding:12px 18px;border-radius:8px;font-size:1.1em">'
                    f'<b>CPA Break-even: ${be_cpa:,.2f} MXN</b> &nbsp;|&nbsp; '
                    f'CPA real: ${pl["cpa_real"]:,.2f} MXN &nbsp;|&nbsp; '
                    f'{"✅ Rentable" if pl["cpa_real"] <= be_cpa else "🔴 Pérdida por orden"}'
                    f'</div>', unsafe_allow_html=True)

    # Simulador ¿Qué pasa si...?
    with st.expander("🧮 Simulador ¿Qué pasa si...?"):
        sc1, sc2, sc3 = st.columns(3)
        sim_cpa = sc1.slider("Nuevo CPA objetivo (MXN)", 100, 2000, int(pl["cpa_real"]), 25)
        sim_aov = sc2.slider("Nuevo AOV (MXN)", 500, 5000, max(int(pl["aov"]), 500), 50)
        sim_pct = sc3.slider("% Entregas", 10, 100, max(int(pl["pct_entrega"]), 10), 1)
        n_real  = max(pl["n_total"], 1)
        cogs_u  = pl["cogs"] / max(pl["n_entregadas"], 1)
        flt_u   = pl["flete"] / max(pl["n_entregadas"], 1)
        sn_ent  = max(n_real * sim_pct / 100, 1)
        srev    = sn_ent * sim_aov
        smb     = srev - cogs_u * sn_ent - flt_u * sn_ent
        sgasto  = sn_ent * sim_cpa
        sutil   = smb - sgasto - pl["costo_devol"]
        smer    = srev / max(sgasto, 1)
        sm1, sm2, sm3, sm4 = st.columns(4)
        sm1.metric("Revenue", fmt(srev), f"{(srev-pl['revenue'])/max(pl['revenue'],1)*100:+.1f}%")
        sm2.metric("Utilidad", fmt(sutil), f"{sutil-pl['utilidad']:+,.0f}",
                   delta_color="normal" if sutil >= pl["utilidad"] else "inverse")
        sm3.metric("MER", f"{smer:.2f}×", f"{smer-pl['mer']:+.2f}",
                   delta_color="normal" if smer >= pl["mer"] else "inverse")
        sm4.metric("Gasto Ads", fmt(sgasto))


# ═══════════════════════════════════════════════════════════════════════════
# TAB 3 — VENTAS
# ═══════════════════════════════════════════════════════════════════════════
with tab_ventas:
    st.markdown("## 📦 Ventas")
    st.caption("Aquí ves el detalle de todos tus pedidos: cuántos llegan, cuántos se cancelan, "
               "cuánto paga cada cliente en promedio y qué ciudades te compran más.")
    if orders.empty:
        st.info("Sin órdenes en el período.")
    elif HAS_PLOTLY:
        v1, v2 = st.columns(2)

        # Órdenes por semana (stacked by status groups)
        with v1:
            st.caption("📊 **Órdenes por semana** — Cada barra es una semana. Verde = llegaron, azul = en camino, "
                       "rojo = canceladas, naranja = devoluciones. Quieres el verde dominando.")
            orders2 = orders.copy()
            orders2["semana"] = orders2["fecha_dt"].dt.to_period("W-SUN").apply(
                lambda p: str(p.start_time.date()) if not pd.isna(p) else None)
            orders2 = orders2.dropna(subset=["semana"])
            def group_status(s):
                if s == "ENTREGADO":   return "Entregada"
                if s == "CANCELADO":   return "Cancelada"
                if s in DEV_ESTADOS:   return "Devolución"
                return "Pipeline"
            orders2["status_group"] = orders2["estatus"].apply(group_status)
            sw = orders2.groupby(["semana", "status_group"]).size().reset_index(name="n")
            color_map = {"Entregada": "#22c55e", "Pipeline": "#60a5fa",
                         "Devolución": "#f97316", "Cancelada": "#ef4444"}
            fig_sw = px.bar(sw, x="semana", y="n", color="status_group",
                            color_discrete_map=color_map, barmode="stack",
                            title="Órdenes por semana",
                            labels={"semana": "", "n": "Órdenes", "status_group": "Estado"})
            fig_sw.update_layout(height=300, margin=dict(t=35, b=30),
                                 legend=dict(orientation="h"))
            st.plotly_chart(fig_sw, use_container_width=True)

        # AOV trend
        with v2:
            st.caption("💵 **Ticket promedio (AOV) semanal** — Cuánto paga en promedio cada cliente que te compra. "
                       "Si sube la línea verde punteada, vende más caro o en packs — más dinero sin más pedidos.")
            aov_w = (orders2[orders2["estatus"] != "CANCELADO"]
                     .groupby("semana")["valor_venta"].mean().reset_index())
            fig_aov = go.Figure(go.Scatter(
                x=aov_w["semana"], y=aov_w["valor_venta"],
                mode="lines+markers", name="AOV",
                line=dict(color="#a78bfa", width=2),
                fill="tozeroy", fillcolor="rgba(167,139,250,0.1)",
            ))
            fig_aov.add_hline(y=BENCHMARKS["aov_objetivo"], line_dash="dash",
                              line_color="green", annotation_text="Objetivo")
            fig_aov.update_layout(title="AOV (ticket promedio) semanal",
                                  height=300, margin=dict(t=35, b=30),
                                  yaxis_title="MXN")
            st.plotly_chart(fig_aov, use_container_width=True)

        st.divider()
        vg1, vg2 = st.columns(2)

        # Mapa por departamento
        with vg1:
            st.subheader("Top estados por volumen")
            st.caption("🗺️ **Revenue por estado** — Los estados más largos son donde más dinero entra. "
                       "Úsalo para poner más presupuesto de anuncios en esas regiones.")
            depto_df = (orders[orders["es_prueba"] == 0]
                        .groupby("departamento")
                        .agg(ordenes=("orden_id", "count"),
                             revenue=("valor_venta", "sum"))
                        .reset_index()
                        .sort_values("revenue", ascending=False)
                        .head(12))
            fig_dep = px.bar(depto_df, x="revenue", y="departamento", orientation="h",
                             color="ordenes", color_continuous_scale="Blues",
                             labels={"revenue": "Revenue (MXN)", "departamento": "",
                                     "ordenes": "Órdenes"},
                             title="Revenue por estado")
            fig_dep.update_layout(height=340, margin=dict(t=35, b=15))
            st.plotly_chart(fig_dep, use_container_width=True)

        # Novedades / motivos de fallo
        with vg2:
            st.subheader("Motivos de no-entrega")
            st.caption("⚠️ **Novedades (problemas en entrega)** — Cada barra es un motivo por el que el paquete no llegó. "
                       "Las más frecuentes son las que debes resolver primero con tu transportadora.")
            nov_df = (orders[orders["novedad"].notna() & (orders["novedad"] != "") &
                             (orders["novedad"] != "nan")]
                      .groupby("novedad").size().reset_index(name="count")
                      .sort_values("count", ascending=False).head(10))
            if not nov_df.empty:
                fig_nov = px.bar(nov_df, x="count", y="novedad", orientation="h",
                                 color="count", color_continuous_scale="Reds",
                                 title="Novedades (top 10)",
                                 labels={"novedad": "", "count": "Cantidad"})
                fig_nov.update_layout(height=340, margin=dict(t=35, b=15))
                st.plotly_chart(fig_nov, use_container_width=True)
            else:
                st.info("Sin novedades registradas.")

        st.divider()
        st.subheader("Top 15 ciudades por revenue entregado")
        st.caption("🏙️ **Las mejores ciudades** — Solo cuentan pedidos que SÍ llegaron al cliente. "
                   "Estas ciudades son tu mercado principal — el lugar donde más clientes reales tienes.")
        ent_geo = orders[(orders["estatus"] == "ENTREGADO") &
                         orders["ciudad_destino"].notna()]
        if not ent_geo.empty:
            city_df = (ent_geo.groupby("ciudad_destino")
                       .agg(revenue=("valor_venta", "sum"),
                            ordenes=("orden_id", "count"),
                            tasa_ent=("valor_venta", "count"))
                       .reset_index().sort_values("revenue", ascending=False).head(15))
            city_df["revenue_fmt"] = city_df["revenue"].apply(lambda x: f"${x:,.0f}")
            st.dataframe(city_df[["ciudad_destino", "ordenes", "revenue_fmt"]].rename(columns={
                "ciudad_destino": "Ciudad", "ordenes": "Órdenes", "revenue_fmt": "Revenue"}),
                hide_index=True, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════
# TAB 4 — MARKETING
# ═══════════════════════════════════════════════════════════════════════════
with tab_mkt:
    st.markdown("## 📣 Marketing")
    st.caption("Aquí ves todo lo que pasó con tus anuncios de Facebook e Instagram. "
               "Te dice si el dinero que gastas en publicidad está trayendo clientes o se está desperdiciando.")
    if meta.empty:
        st.info("Sin datos de Meta Ads para el período seleccionado.")
    else:
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("💶 Gasto Total", f"€{meta['gasto_eur'].sum():.0f}",
                  help="Lo que pagaste en Facebook/Instagram para conseguir clientes.")
        m2.metric("👁️ Impresiones",  f"{meta['impresiones'].sum():,.0f}",
                  help="Cuántas veces apareció tu anuncio en la pantalla de alguien (contando repeticiones).")
        m3.metric("👥 Alcance",      f"{meta['alcance'].sum():,.0f}",
                  help="Cuántas personas DISTINTAS vieron tu anuncio al menos una vez.")
        m4.metric("🖱️ Clics",        f"{meta['clics'].sum():,.0f}",
                  help="Cuántas personas hicieron click en tu anuncio para ir a tu tienda.")
        m5.metric("🛒 Compras Meta", f"{meta['compras_meta'].sum():.0f}",
                  help="Compras que Meta dice que vinieron de tus anuncios. Puede diferir de Dropi — confía más en Dropi.")
        m6.metric("📊 CPM prom",     f"€{meta['cpm'].mean():.2f}",
                  help="Cuánto pagas por que 1,000 personas vean tu anuncio. Si sube mucho, hay mucha competencia o tu anuncio tiene mala puntuación.")

        if HAS_PLOTLY:
            st.divider()
            mp1, mp2 = st.columns(2)

            # ROAS por campaña
            with mp1:
                st.caption("📊 **ROAS por campaña** — Las barras más largas son tus campañas más eficientes. "
                           "ROAS >2.5× = rentable. ROAS <1.5× = esa campaña te está costando dinero.")
                camp_agg = (meta.groupby("campana")
                            .agg(gasto=("gasto_eur", "sum"),
                                 compras=("compras_meta", "sum"),
                                 roas=("roas_meta", "mean"),
                                 clics=("clics", "sum"))
                            .reset_index().sort_values("gasto", ascending=False).head(10))
                fig_roas = px.bar(camp_agg, x="roas", y="campana", orientation="h",
                                  color="gasto", color_continuous_scale="Purples",
                                  title="ROAS Meta por campaña (top 10)",
                                  labels={"roas": "ROAS", "campana": "", "gasto": "Gasto (€)"})
                fig_roas.update_layout(height=320, margin=dict(t=35, b=15))
                st.plotly_chart(fig_roas, use_container_width=True)

            # Gasto por producto (pie)
            with mp2:
                st.caption("🍩 **Gasto por producto** — Qué porcentaje de tu presupuesto de anuncios va a cada producto. "
                           "Si un solo producto tiene >80%, eso es riesgo — si falla, caes entero.")
                prod_agg = meta.groupby("producto")["gasto_eur"].sum().reset_index()
                fig_prod_pie = px.pie(prod_agg, values="gasto_eur", names="producto",
                                     title="Distribución de gasto por producto",
                                     color_discrete_sequence=px.colors.qualitative.Set2,
                                     hole=0.4)
                fig_prod_pie.update_layout(height=320, margin=dict(t=35, b=15))
                st.plotly_chart(fig_prod_pie, use_container_width=True)

            st.divider()
            mp3, mp4 = st.columns(2)

            # Funnel impresiones → compras
            with mp3:
                st.caption("🔽 **Funnel de anuncios** — Cada capa es un filtro: de todos los que vieron el anuncio, "
                           "¿cuántos hicieron click? ¿cuántos compraron? Cuanto más ancho en la base, mejor convierte tu anuncio.")
                funnel_meta = {
                    "Impresiones": int(meta["impresiones"].sum()),
                    "Alcance":     int(meta["alcance"].sum()),
                    "Clics":       int(meta["clics"].sum()),
                    "Carritos":    int(meta["carritos"].sum()),
                    "Compras Meta": int(meta["compras_meta"].sum()),
                }
                fig_fun = go.Figure(go.Funnel(
                    y=list(funnel_meta.keys()),
                    x=list(funnel_meta.values()),
                    textinfo="value+percent initial",
                    marker_color=["#60a5fa","#818cf8","#a78bfa","#c084fc","#e879f9"],
                ))
                fig_fun.update_layout(title="Funnel Meta Ads", height=280,
                                      margin=dict(t=35, b=15))
                st.plotly_chart(fig_fun, use_container_width=True)

            # CPM / CPC trend
            with mp4:
                st.caption("📉 **CPM y CPC semanal** — CPM es cuánto pagas por 1,000 vistas. CPC es cuánto pagas por cada click. "
                           "Si ambas líneas suben, los anuncios se están encareciendo — hay que optimizar o crear creativos nuevos.")
                meta_w = meta.copy()
                meta_w["semana"] = meta_w["fecha_dt"].dt.to_period("W-SUN").apply(
                    lambda p: str(p.start_time.date()) if not pd.isna(p) else None)
                meta_w = meta_w.dropna(subset=["semana"])
                cost_w = meta_w.groupby("semana").agg(
                    cpm=("cpm", "mean"), cpc=("cpc", "mean"), gasto=("gasto_eur", "sum")
                ).reset_index()
                fig_cost = make_subplots(specs=[[{"secondary_y": True}]])
                fig_cost.add_trace(go.Scatter(x=cost_w["semana"], y=cost_w["cpm"],
                    name="CPM €", line=dict(color="#8b5cf6"), mode="lines+markers"),
                    secondary_y=False)
                fig_cost.add_trace(go.Scatter(x=cost_w["semana"], y=cost_w["cpc"],
                    name="CPC €", line=dict(color="#f97316"), mode="lines+markers"),
                    secondary_y=True)
                fig_cost.update_layout(title="CPM (izq.) y CPC (der.) semanal",
                                       height=280, margin=dict(t=35, b=15), showlegend=True)
                st.plotly_chart(fig_cost, use_container_width=True)

            # Video engagement
            if meta["video_25pct"].sum() > 0:
                st.divider()
                st.subheader("📹 Engagement de video")
                st.caption("🎬 **Retención de video** — De cada 100 personas que vieron tu anuncio en video, "
                           "¿cuántas llegaron al 25% o al 50%? Si la retención es baja, los primeros segundos no enganchan.")
                vid_prod = meta.groupby("producto").agg(
                    vid25=("video_25pct", "sum"),
                    vid50=("video_50pct", "sum"),
                    avg_play=("avg_video_play", "mean"),
                    impresiones=("impresiones", "sum"),
                ).reset_index()
                vid_prod["retención_25pct"] = vid_prod["vid25"] / vid_prod["impresiones"].clip(lower=1) * 100
                vid_prod["retención_50pct"] = vid_prod["vid50"] / vid_prod["impresiones"].clip(lower=1) * 100
                st.dataframe(vid_prod.rename(columns={
                    "producto": "Producto", "vid25": "Views 25%", "vid50": "Views 50%",
                    "avg_play": "Play prom (s)", "retención_25pct": "Ret. 25% (%)",
                    "retención_50pct": "Ret. 50% (%)"}
                ).drop(columns="impresiones"), hide_index=True, use_container_width=True)

        st.divider()
        st.subheader("Tabla completa de campañas")
        st.caption("📋 **Todas tus campañas** — Cada fila es un anuncio. Ordena por Gasto para ver cuáles se llevan más presupuesto "
                   "y compara su ROAS para saber si ese gasto vale la pena.")
        camp_full = (meta.groupby(["campana", "anuncio", "producto"])
                     .agg(gasto=("gasto_eur", "sum"), compras=("compras_meta", "sum"),
                          roas=("roas_meta", "mean"), clics=("clics", "sum"),
                          ctr=("ctr", "mean"), cpc=("cpc", "mean"),
                          carritos=("carritos", "sum"), alcance=("alcance", "sum"))
                     .reset_index().sort_values("gasto", ascending=False))
        camp_full["gasto"] = camp_full["gasto"].apply(lambda x: f"€{x:.2f}")
        camp_full["roas"]  = camp_full["roas"].apply(lambda x: f"{x:.2f}×")
        camp_full["ctr"]   = camp_full["ctr"].apply(lambda x: f"{x:.2f}%")
        camp_full["cpc"]   = camp_full["cpc"].apply(lambda x: f"€{x:.3f}")
        st.dataframe(camp_full.rename(columns={
            "campana": "Campaña", "anuncio": "Anuncio", "producto": "Producto",
            "gasto": "Gasto €", "compras": "Compras", "roas": "ROAS",
            "clics": "Clics", "ctr": "CTR", "cpc": "CPC",
            "carritos": "Carritos", "alcance": "Alcance"}),
            hide_index=True, use_container_width=True)

        # Comparador de anuncios
        with st.expander("⚡ Comparador de anuncios"):
            anuncios_list = meta["anuncio"].dropna().unique().tolist()
            if len(anuncios_list) >= 2:
                cca, ccb = st.columns(2)
                ad_a = cca.selectbox("Anuncio A", anuncios_list, key="cmpA")
                ad_b = ccb.selectbox("Anuncio B", anuncios_list, index=min(1, len(anuncios_list)-1), key="cmpB")
                if ad_a != ad_b:
                    def ad_stats(name):
                        df = meta[meta["anuncio"] == name]
                        return {
                            "gasto":    float(df["gasto_eur"].sum()),
                            "compras":  float(df["compras_meta"].sum()),
                            "roas":     float(df["roas_meta"].mean() or 0),
                            "clics":    float(df["clics"].sum()),
                            "ctr":      float(df["ctr"].mean() or 0),
                            "cpc":      float(df["cpc"].mean() or 0),
                            "carritos": float(df["carritos"].sum()),
                        }
                    sa, sb = ad_stats(ad_a), ad_stats(ad_b)
                    metricas_cmp = [("Gasto €","gasto","lower"),("Compras","compras","higher"),
                                    ("ROAS","roas","higher"),("Clics","clics","higher"),
                                    ("CTR %","ctr","higher"),("CPC €","cpc","lower"),
                                    ("Carritos","carritos","higher")]
                    rows, score_a, score_b = [], 0, 0
                    for lbl, key, direction in metricas_cmp:
                        va, vb = sa[key], sb[key]
                        if direction == "higher":
                            w = "A 🟢" if va > vb else ("B 🟢" if vb > va else "—")
                            if va > vb: score_a += 1
                            elif vb > va: score_b += 1
                        else:
                            w = "A 🟢" if va < vb else ("B 🟢" if vb < va else "—")
                            if va < vb: score_a += 1
                            elif vb < va: score_b += 1
                        rows.append({"Métrica": lbl, f"A: {ad_a[:25]}": f"{va:.3f}",
                                     f"B: {ad_b[:25]}": f"{vb:.3f}", "Ganador": w})
                    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
                    veredicto = (f"🏆 {ad_a} gana ({score_a}/{len(metricas_cmp)} métricas)" if score_a > score_b
                                 else f"🏆 {ad_b} gana ({score_b}/{len(metricas_cmp)} métricas)" if score_b > score_a
                                 else "🤝 Empate")
                    vc = "#ccffcc" if score_a != score_b else "#fff3cc"
                    st.markdown(f'<div style="background:{vc};padding:10px 16px;border-radius:8px;font-weight:bold">{veredicto}</div>',
                                unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# TAB 5 — PRODUCTOS
# ═══════════════════════════════════════════════════════════════════════════
with tab_prod:
    st.markdown("## 🛍️ Productos")
    st.caption("Compara el rendimiento de cada producto en tus anuncios. "
               "Te dice qué producto te da más ventas por cada euro invertido y cuál necesitas mejorar o pausar.")
    if meta.empty:
        st.info("Sin datos de Meta para análisis de productos.")
    else:
        prod_meta = meta.groupby("producto").agg(
            gasto_eur=("gasto_eur", "sum"),
            compras_meta=("compras_meta", "sum"),
            roas_meta=("roas_meta", "mean"),
            clics=("clics", "sum"),
            ctr=("ctr", "mean"),
            cpc=("cpc", "mean"),
            alcance=("alcance", "sum"),
            dias=("fecha_dt", "nunique"),
        ).reset_index()
        prod_meta["cpa_meta"] = prod_meta["gasto_eur"] / prod_meta["compras_meta"].clip(lower=1)
        prod_meta["gasto_mxn"] = prod_meta["gasto_eur"] * tasa

        # Semáforos por producto
        prod_meta["ROAS"] = prod_meta["roas_meta"].apply(
            lambda x: "🟢" if x >= 2.5 else ("🟡" if x >= 1.5 else "🔴"))
        prod_meta["CTR"] = prod_meta["ctr"].apply(
            lambda x: "🟢" if x >= 2.0 else ("🟡" if x >= 1.0 else "🔴"))

        if HAS_PLOTLY:
            pp1, pp2 = st.columns(2)
            with pp1:
                st.caption("📊 **Gasto por producto (color = ROAS)** — Verde = producto rentable con buen ROAS. "
                           "Rojo = gastas mucho pero convierte poco. Esos son los que necesitas optimizar o pausar.")
                fig_p1 = px.bar(prod_meta.sort_values("gasto_eur", ascending=False),
                                x="producto", y="gasto_eur",
                                color="roas_meta", color_continuous_scale="RdYlGn",
                                range_color=[0, 4],
                                title="Gasto por producto (coloreado por ROAS Meta)",
                                labels={"gasto_eur": "Gasto €", "producto": ""})
                fig_p1.update_layout(height=300, margin=dict(t=35, b=15))
                st.plotly_chart(fig_p1, use_container_width=True)
            with pp2:
                st.caption("🔵 **Gasto vs ROAS (burbuja = compras)** — Los productos en la parte superior derecha son los ganadores: "
                           "gastaste mucho y recuperaste bien. Los que están abajo de la línea verde punteada están perdiendo dinero.")
                # Scatter: Gasto vs ROAS
                fig_p2 = px.scatter(prod_meta, x="gasto_eur", y="roas_meta",
                                    size="compras_meta", color="producto",
                                    text="producto",
                                    title="Gasto vs ROAS por producto (burbuja = compras)",
                                    labels={"gasto_eur": "Gasto €", "roas_meta": "ROAS Meta"})
                fig_p2.add_hline(y=2.5, line_dash="dash", line_color="green", annotation_text="ROAS objetivo")
                fig_p2.update_traces(textposition="top center")
                fig_p2.update_layout(height=300, margin=dict(t=35, b=15))
                st.plotly_chart(fig_p2, use_container_width=True)

        st.subheader("Resumen por producto")
        st.caption("📋 **Tabla de productos** — Las columnas 🚦 son semáforos: 🟢 = bien, 🟡 = cuidado, 🔴 = problema. "
                   "Si un producto tiene ROAS 🔴 y CTR 🔴, considera pausarlo y probar creativos nuevos.")
        prod_show = prod_meta.copy()
        prod_show["gasto_eur"]     = prod_show["gasto_eur"].apply(lambda x: f"€{x:.2f}")
        prod_show["roas_meta"]     = prod_show["roas_meta"].apply(lambda x: f"{x:.2f}×")
        prod_show["ctr"]           = prod_show["ctr"].apply(lambda x: f"{x:.2f}%")
        prod_show["cpc"]           = prod_show["cpc"].apply(lambda x: f"€{x:.3f}")
        prod_show["cpa_meta"]      = prod_show["cpa_meta"].apply(lambda x: f"€{x:.2f}")
        prod_show["alcance"]       = prod_show["alcance"].apply(lambda x: f"{x:,.0f}")
        st.dataframe(prod_show[["producto","gasto_eur","compras_meta","roas_meta","ROAS",
                                 "ctr","CTR","cpc","cpa_meta","alcance","dias"]].rename(columns={
            "producto":"Producto","gasto_eur":"Gasto €","compras_meta":"Compras",
            "roas_meta":"ROAS Meta","ROAS":"🚦","ctr":"CTR","CTR":"🚦 CTR",
            "cpc":"CPC €","cpa_meta":"CPA €","alcance":"Alcance","dias":"Días activo"}),
            hide_index=True, use_container_width=True)

        if HAS_PLOTLY:
            st.divider()
            st.caption("📈 **Inversión semanal por producto** — Cada color es un producto. Cuando una área crece, "
                       "estás gastando más en ese producto. Úsalo para ver si cambiaste de estrategia semana a semana.")
            # Timeline por producto
            meta_daily = meta.copy()
            meta_daily["semana"] = meta_daily["fecha_dt"].dt.to_period("W-SUN").apply(
                lambda p: str(p.start_time.date()) if not pd.isna(p) else None)
            prod_timeline = (meta_daily.dropna(subset=["semana"])
                             .groupby(["semana","producto"])["gasto_eur"].sum().reset_index())
            fig_tl = px.area(prod_timeline, x="semana", y="gasto_eur", color="producto",
                             title="Inversión semanal por producto",
                             labels={"semana":"","gasto_eur":"Gasto €","producto":"Producto"})
            fig_tl.update_layout(height=280, margin=dict(t=35, b=15),
                                 legend=dict(orientation="h"))
            st.plotly_chart(fig_tl, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════
# TAB 6 — LOGÍSTICA
# ═══════════════════════════════════════════════════════════════════════════
with tab_logistica:
    st.markdown("## 🚚 Logística")
    st.caption("Aquí ves el rendimiento de cada transportadora (mensajero). "
               "En COD, el mensajero es clave: si no entrega, no cobras. Esta pestaña te dice cuál funciona y cuál hay que cambiar.")
    st.info("💡 **Novedad** = cuando hay un problema con la entrega (dirección incorrecta, cliente no disponible, etc.). Cuantas menos novedades, mejor.")
    if orders.empty:
        st.info("Sin órdenes para el período.")
    else:
        real_orders = orders[orders["es_prueba"] == 0]
        carrier_agg = (real_orders.groupby("transportadora").agg(
            total=("orden_id", "count"),
            entregadas=("estatus", lambda x: (x == "ENTREGADO").sum()),
            canceladas=("estatus", lambda x: (x == "CANCELADO").sum()),
            devoluciones=("estatus", lambda x: x.isin(DEV_ESTADOS).sum()),
            costo_hundido=("costo_devolucion", "sum"),
            revenue=("valor_venta", lambda x: x[real_orders.loc[x.index,"estatus"] == "ENTREGADO"].sum()),
        ).reset_index())
        carrier_agg["pct_entrega"] = carrier_agg["entregadas"] / carrier_agg["total"].clip(lower=1) * 100
        carrier_agg["pct_cancel"]  = carrier_agg["canceladas"] / carrier_agg["total"].clip(lower=1) * 100

        if HAS_PLOTLY:
            lc1, lc2 = st.columns(2)
            with lc1:
                st.caption("📊 **% Entrega por carrier** — Verde = bueno (>70%), naranja = aceptable, rojo = hay que cambiar. "
                           "Si una barra está roja, ese mensajero te está costando dinero con cada paquete que no entrega.")
                colors_carr = ["#22c55e" if v >= 70 else "#f97316" if v >= 40 else "#ef4444"
                               for v in carrier_agg["pct_entrega"]]
                fig_carr = go.Figure(go.Bar(
                    x=carrier_agg["transportadora"], y=carrier_agg["pct_entrega"],
                    marker_color=colors_carr,
                    text=carrier_agg["pct_entrega"].apply(lambda x: f"{x:.1f}%"),
                    textposition="outside",
                ))
                fig_carr.add_hline(y=70, line_dash="dash", line_color="green",
                                   annotation_text="Objetivo 70%")
                fig_carr.update_layout(title="% Entrega por carrier", height=300,
                                       margin=dict(t=35, b=15), yaxis_title="% Entrega")
                st.plotly_chart(fig_carr, use_container_width=True)

            with lc2:
                st.caption("💸 **Costo hundido por carrier** — Es el dinero que perdiste en devoluciones con cada mensajero. "
                           "Costo hundido = ya lo pagaste y no lo recuperas. El que más barra tenga está dañando más tu margen.")
                fig_cost = go.Figure(go.Bar(
                    x=carrier_agg["transportadora"], y=carrier_agg["costo_hundido"],
                    marker_color="#ef4444",
                    text=carrier_agg["costo_hundido"].apply(lambda x: f"${x:,.0f}"),
                    textposition="outside",
                ))
                fig_cost.update_layout(title="Costo hundido (devoluciones) por carrier",
                                       height=300, margin=dict(t=35, b=15), yaxis_title="MXN")
                st.plotly_chart(fig_cost, use_container_width=True)

        st.subheader("Matriz de carriers")
        st.caption("📋 **Tabla de transportadoras** — Compara de un vistazo todos los mensajeros. "
                   "Busca el que tenga mayor '% Entrega' y menor 'Costo Hundido' — ese es tu mejor aliado.")
        carrier_show = carrier_agg.copy()
        carrier_show["pct_entrega"] = carrier_show["pct_entrega"].apply(lambda x: f"{x:.1f}%")
        carrier_show["pct_cancel"]  = carrier_show["pct_cancel"].apply(lambda x: f"{x:.1f}%")
        carrier_show["costo_hundido"] = carrier_show["costo_hundido"].apply(lambda x: f"${x:,.0f}")
        carrier_show["revenue"]       = carrier_show["revenue"].apply(lambda x: f"${x:,.0f}")
        st.dataframe(carrier_show.rename(columns={
            "transportadora":"Carrier","total":"Total","entregadas":"Entregadas",
            "canceladas":"Canceladas","devoluciones":"Dev.","pct_entrega":"% Entrega",
            "pct_cancel":"% Cancel","costo_hundido":"Costo Hundido","revenue":"Revenue"}),
            hide_index=True, use_container_width=True)

        if HAS_PLOTLY:
            st.divider()
            lc3, lc4 = st.columns(2)

            # Tendencia de entrega semanal por carrier
            with lc3:
                st.caption("📈 **% Entrega semanal por carrier** — ¿Está mejorando o empeorando cada mensajero semana a semana? "
                           "Una línea que baja es una señal de alarma: llama a ese carrier antes de mandar más pedidos.")
                orders_w = real_orders.copy()
                orders_w["semana"] = orders_w["fecha_dt"].dt.to_period("W-SUN").apply(
                    lambda p: str(p.start_time.date()) if not pd.isna(p) else None)
                orders_w = orders_w.dropna(subset=["semana"])
                top_carriers = carrier_agg.nlargest(4, "total")["transportadora"].tolist()
                ent_rate_w = []
                for car in top_carriers:
                    df_c = orders_w[orders_w["transportadora"] == car]
                    agg  = df_c.groupby("semana").agg(
                        total=("orden_id","count"),
                        entregadas=("estatus", lambda x: (x=="ENTREGADO").sum())
                    ).reset_index()
                    agg["pct"] = agg["entregadas"] / agg["total"].clip(lower=1) * 100
                    agg["carrier"] = car
                    ent_rate_w.append(agg)
                if ent_rate_w:
                    ew = pd.concat(ent_rate_w)
                    fig_ew = px.line(ew, x="semana", y="pct", color="carrier",
                                    markers=True, title="% Entrega semanal por carrier",
                                    labels={"pct":"% Entrega","semana":"","carrier":"Carrier"})
                    fig_ew.add_hline(y=70, line_dash="dash", line_color="green")
                    fig_ew.update_layout(height=280, margin=dict(t=35, b=15))
                    st.plotly_chart(fig_ew, use_container_width=True)

            # Novedades por tipo y carrier
            with lc4:
                st.caption("⚠️ **Novedades por carrier** — Los problemas de entrega desglosados por mensajero. "
                           "Si un carrier tiene muchas novedades del mismo tipo, eso es un patrón que debes reclamar.")
                nov_carrier = (real_orders[
                    real_orders["novedad"].notna() & (real_orders["novedad"] != "") &
                    (real_orders["novedad"] != "nan")]
                    .groupby(["transportadora","novedad"]).size().reset_index(name="n")
                    .sort_values("n", ascending=False).head(15))
                if not nov_carrier.empty:
                    fig_nc = px.bar(nov_carrier, x="n", y="novedad", color="transportadora",
                                   orientation="h", title="Novedades por carrier",
                                   labels={"novedad":"","n":"Cantidad","transportadora":"Carrier"})
                    fig_nc.update_layout(height=280, margin=dict(t=35, b=15))
                    st.plotly_chart(fig_nc, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════
# TAB 7 — QUARTERS
# ═══════════════════════════════════════════════════════════════════════════
with tab_q:
    st.markdown("## 📅 Quarters (Trimestres)")
    st.caption("Un quarter es un período de 3 meses (Q1 = enero-marzo, Q2 = abril-junio, etc.). "
               "Esta pestaña te muestra cómo ha evolucionado tu negocio cada trimestre para ver si estás creciendo.")
    st.info("📅 Esta pestaña muestra TODO el historial desde que empezaste. El filtro de fechas no aplica aquí — sirve para ver la evolución completa del negocio.")
    # Quarters muestra TODA la historia (sin filtro de fechas, eso es correcto)
    # pero sí respeta los filtros de producto y transportadora.
    orders_q = all_orders_raw[all_orders_raw["es_prueba"] == 0].copy()
    meta_q   = all_meta_raw.copy()
    # Aplicar filtro de transportadora
    if sel_carrier:
        orders_q = orders_q[orders_q["transportadora"].isin(sel_carrier)]
    # Aplicar filtro de producto → tienda
    if sel_prod and "tienda" in orders_q.columns:
        _tiendas_q = {_PROD_TIENDA[p] for p in sel_prod if p in _PROD_TIENDA}
        if _tiendas_q:
            orders_q = orders_q[orders_q["tienda"].isin(_tiendas_q)]
        meta_q = meta_q[meta_q["producto"].isin(sel_prod)]
    orders_q["quarter"] = orders_q["fecha_dt"].dt.to_period("Q").astype(str)
    meta_q["quarter"]   = meta_q["fecha_dt"].dt.to_period("Q").astype(str)

    quarters = sorted(orders_q["quarter"].dropna().unique().tolist())

    if not quarters:
        st.info("Sin datos para vista por quarters.")
    else:
        q_data = []
        for q in quarters:
            oq = orders_q[orders_q["quarter"] == q]
            mq = meta_q[meta_q["quarter"] == q]
            pq = compute_pl(oq, mq, tasa)
            q_data.append({"Quarter": q, **pq})
        q_df = pd.DataFrame(q_data)

        # KPI table por quarter
        st.subheader("Comparativa por Quarter")
        st.caption("📋 **Tabla por trimestre** — Cada fila es un período de 3 meses. "
                   "Compara Revenue y Utilidad entre trimestres para ver si el negocio está creciendo o estancado.")
        q_show = q_df[["Quarter","revenue","utilidad","mer","roas_real","cpa_real",
                        "pct_entrega","pct_cancel","n_total","n_entregadas","gasto_mxn"]].copy()
        for col in ["revenue","utilidad","gasto_mxn"]:
            q_show[col] = q_show[col].apply(lambda x: f"${x:,.0f}")
        for col in ["mer","roas_real"]:
            q_show[col] = q_show[col].apply(lambda x: f"{x:.2f}×")
        q_show["cpa_real"]   = q_show["cpa_real"].apply(lambda x: f"${x:,.0f}")
        q_show["pct_entrega"]= q_show["pct_entrega"].apply(lambda x: f"{x:.1f}%")
        q_show["pct_cancel"] = q_show["pct_cancel"].apply(lambda x: f"{x:.1f}%")
        st.dataframe(q_show.rename(columns={
            "revenue":"Revenue","utilidad":"Utilidad","mer":"MER","roas_real":"ROAS",
            "cpa_real":"CPA","pct_entrega":"% Entrega","pct_cancel":"% Cancel",
            "n_total":"Órdenes","n_entregadas":"Entregadas","gasto_mxn":"Gasto MXN"}),
            hide_index=True, use_container_width=True)

        if HAS_PLOTLY and len(q_df) > 0:
            st.divider()
            qc1, qc2 = st.columns(2)

            with qc1:
                st.caption("📊 **Revenue y Utilidad por trimestre** — Las barras azules son ventas totales, "
                           "las verdes/rojas son ganancia. Si la barra roja crece entre trimestres, el negocio está yendo hacia atrás.")
                util_colors = ["#ef4444" if v < 0 else "#22c55e" for v in q_df["utilidad"]]
                fig_qu = go.Figure()
                fig_qu.add_bar(x=q_df["Quarter"], y=q_df["revenue"], name="Revenue", marker_color="#60a5fa")
                fig_qu.add_bar(x=q_df["Quarter"], y=q_df["utilidad"], name="Utilidad", marker_color=util_colors)
                fig_qu.update_layout(title="Revenue y Utilidad por Quarter",
                                     barmode="group", height=300, margin=dict(t=35, b=15))
                st.plotly_chart(fig_qu, use_container_width=True)

            with qc2:
                st.caption("📉 **MER y ROAS por trimestre** — ¿Tus anuncios se vuelven más o menos eficientes con el tiempo? "
                           "Si las líneas suben trimestre a trimestre, estás mejorando. Si bajan, hay que revisar los creativos.")
                fig_qmer = go.Figure()
                fig_qmer.add_scatter(x=q_df["Quarter"], y=q_df["mer"], name="MER",
                                     mode="lines+markers+text", text=q_df["mer"].apply(lambda x: f"{x:.2f}×"),
                                     textposition="top center", line=dict(color="#8b5cf6", width=3),
                                     marker=dict(size=10))
                fig_qmer.add_scatter(x=q_df["Quarter"], y=q_df["roas_real"], name="ROAS Real",
                                     mode="lines+markers+text", text=q_df["roas_real"].apply(lambda x: f"{x:.2f}×"),
                                     textposition="top center", line=dict(color="#f97316", width=3, dash="dot"),
                                     marker=dict(size=10))
                fig_qmer.add_hline(y=2.0, line_dash="dash", line_color="red", annotation_text="MER mín")
                fig_qmer.update_layout(title="MER y ROAS por Quarter",
                                       height=300, margin=dict(t=35, b=15))
                st.plotly_chart(fig_qmer, use_container_width=True)

            # Top semana por quarter
            st.divider()
            st.subheader("Mejor semana por Quarter")
            st.caption("🏆 **Tu mejor semana en cada trimestre** — Guarda estas fechas como referencia. "
                       "¿Qué hiciste diferente esa semana? Eso es lo que debes repetir.")
            semanas_q = semanas_df.copy()
            semanas_q["quarter"] = semanas_q["fecha_dt"].dt.to_period("Q").astype(str)
            best_weeks = (semanas_q.sort_values("utilidad_neta", ascending=False)
                          .groupby("quarter").first().reset_index()
                          [["quarter","fecha_reporte","ventas_confirmadas","utilidad_neta","mer","roas_real"]])
            best_weeks["utilidad_neta"] = best_weeks["utilidad_neta"].apply(lambda x: f"${x:,.0f}")
            best_weeks["ventas_confirmadas"] = best_weeks["ventas_confirmadas"].apply(lambda x: f"${x:,.0f}")
            best_weeks["mer"] = best_weeks["mer"].apply(lambda x: f"{x:.2f}×")
            best_weeks["roas_real"] = best_weeks["roas_real"].apply(lambda x: f"{x:.2f}×")
            st.dataframe(best_weeks.rename(columns={
                "quarter":"Quarter","fecha_reporte":"Semana","ventas_confirmadas":"Revenue",
                "utilidad_neta":"Utilidad","mer":"MER","roas_real":"ROAS"}),
                hide_index=True, use_container_width=True)

            # Gasto Meta por quarter y producto
            if not meta_q.empty:
                st.divider()
                st.caption("📊 **Inversión por trimestre y producto** — Cada color es un producto. "
                           "Si la barra de un producto desaparece en un trimestre, ese período lo dejaste de anunciar.")
                prod_q_agg = (meta_q.dropna(subset=["quarter"])
                              .groupby(["quarter","producto"])["gasto_eur"].sum().reset_index())
                fig_pq = px.bar(prod_q_agg, x="quarter", y="gasto_eur", color="producto",
                                barmode="stack", title="Inversión por Quarter y Producto",
                                labels={"gasto_eur":"Gasto €","quarter":"","producto":"Producto"},
                                color_discrete_sequence=px.colors.qualitative.Set2)
                fig_pq.update_layout(height=280, margin=dict(t=35, b=15),
                                     legend=dict(orientation="h"))
                st.plotly_chart(fig_pq, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════
# TAB 8 — CARTERA
# ═══════════════════════════════════════════════════════════════════════════
with tab_cartera_tab:
    st.markdown("## 💳 Cartera Dropi")
    st.caption("La cartera es la 'alcancía' de Dropi donde guarda tu dinero. "
               "Aquí ves todo el historial de entradas (ganancias) y salidas (fletes, retiros) de esa cuenta.")
    if not cartera_res:
        st.info("Carga el historial de cartera desde el sidebar (💳 Cartera .xlsx).")
    else:
        st.subheader("Resumen Cartera Dropi")
        ck1, ck2, ck3, ck4 = st.columns(4)
        ck1.metric("💳 Saldo actual", f"${cartera_res['saldo_actual']:,.2f} MXN",
                   help="Lo que tienes acumulado en la cartera de Dropi ahora mismo. Puedes pedirlo a tu banco cuando quieras — eso se llama 'retiro'.")
        ck2.metric("📥 Ganancias acreditadas", f"${cartera_res['total_ganancias']:,.2f} MXN",
                   help="Todo lo que Dropi te ha acreditado por pedidos entregados desde el inicio. Es el total histórico, no solo el período seleccionado.")
        ck3.metric("📤 Retiros realizados", f"${cartera_res['retiros_realizados']:,.2f} MXN",
                   help="El dinero que ya sacaste de Dropi a tu banco personal.")
        ck4.metric("📦 Fletes cobrados", f"${cartera_res['fletes_cobrados']:,.2f} MXN",
                   help="Lo que Dropi te ha cobrado por generar guías (el costo del mensajero cuando creas el envío).")
        ck5, ck6, ck7, ck8 = st.columns(4)
        ck5.metric("↩️ Cobros devolución", f"${cartera_res['cobros_devolucion']:,.2f} MXN",
                   help="Cuando el cliente rechazó o regresó el paquete. Te cuesta el flete de vuelta sin recibir ganancia.")
        ck6.metric("🔁 Transferencias", f"${cartera_res['transferencias']:,.2f} MXN",
                   help="Dinero transferido dentro de la plataforma Dropi.")
        ck7.metric("⚙️ Ajustes admin", f"${cartera_res['ajustes_admin']:,.2f} MXN",
                   help="Ajustes que hace Dropi directamente en tu cuenta (correcciones, bonos, cargos administrativos).")
        ck8.metric("📋 Movimientos", cartera_res["n_transacciones"],
                   help="Total de entradas y salidas registradas en tu historial de cartera.")

        st.divider()

        # ── Caja consolidada ─────────────────────────────────────────────
        st.subheader("💰 Caja consolidada")
        _db  = st.session_state.caja_banco_debito
        _cl  = st.session_state.caja_banco_credito
        _cf  = st.session_state.caja_credito_favor
        _dr  = cartera_res.get("saldo_actual", 0)
        _tot = _dr + _db + _cl + _cf

        cj1, cj2, cj3, cj4, cj5 = st.columns(5)
        cj1.metric("💳 Dropi",           f"${_dr:,.2f}")
        cj2.metric("🏦 Banco débito",    f"${_db:,.2f}")
        cj3.metric("💳 Crédito dispon.", f"${_cl:,.2f}")
        cj4.metric("✅ Saldo a favor",   f"${_cf:,.2f}")
        cj5.metric("🏦 **TOTAL CAJA**",  f"${_tot:,.2f}",
                   help="Suma de Dropi + banco + crédito disponible + saldo a favor")

        caja_color = "#ccffcc" if _tot >= 10000 else ("#fff3cc" if _tot >= 5000 else "#ffcccc")
        st.markdown(
            f'<div style="background:{caja_color};padding:12px 18px;border-radius:8px;'
            f'font-size:1.1em;font-weight:bold">💰 Caja total: ${_tot:,.2f} MXN &nbsp;·&nbsp; '
            f'<span style="font-weight:normal;font-size:.9em">'
            f'Dropi ${_dr:,.0f} + Banco ${_db:,.0f} + Crédito ${_cl:,.0f} + Favor ${_cf:,.0f}'
            f'</span></div>',
            unsafe_allow_html=True,
        )
        st.caption("Actualiza los saldos externos en el sidebar → 🏦 Caja externa")

        st.divider()

        recon_delta = cartera_res["total_ganancias"] - pl["revenue"]
        pct_ok = abs(recon_delta) < pl["revenue"] * 0.05 if pl["revenue"] > 0 else True
        st.markdown(
            f"""<div style="background:{'#ccffcc' if pct_ok else '#fff3cc'};padding:12px 18px;border-radius:8px">
            <b>Ganancias Dropi (cartera):</b> ${cartera_res['total_ganancias']:,.2f} MXN &nbsp;|&nbsp;
            <b>Revenue P&L período:</b> ${pl['revenue']:,.2f} MXN &nbsp;|&nbsp;
            <b>Diferencia:</b> ${recon_delta:,.2f} MXN &nbsp;
            {'✅ ±5% OK' if pct_ok else '⚠️ Brecha >5% — revisar período del archivo'}
            </div>""", unsafe_allow_html=True)
        st.caption("La cartera cubre todo el historial cargado; el P&L respeta el filtro de fecha activo.")

        if HAS_PLOTLY:
            st.divider()
            st.caption("📈 **Evolución de la cartera** — La línea verde muestra cómo creció (o bajó) tu saldo en Dropi con el tiempo. "
                       "Las barras de abajo muestran qué tipo de movimiento causó cada cambio.")
            df_chart = cartera_df.copy()
            df_chart["fecha_dt2"] = pd.to_datetime(df_chart["fecha"], dayfirst=True, errors="coerce")
            df_chart = df_chart.dropna(subset=["fecha_dt2"]).sort_values("fecha_dt2")
            df_chart["monto_signed"] = df_chart.apply(
                lambda r: r["monto"] if r["tipo"] == "ENTRADA" else -r["monto"], axis=1)
            df_chart["saldo_acum"] = df_chart["monto_previo"] + df_chart["monto_signed"]

            fig_c = make_subplots(rows=2, cols=1, row_heights=[0.6, 0.4],
                                  subplot_titles=["Saldo acumulado (MXN)", "Movimientos por tipo"],
                                  vertical_spacing=0.15)
            fig_c.add_trace(go.Scatter(
                x=df_chart["fecha_dt2"], y=df_chart["saldo_acum"],
                mode="lines+markers", name="Saldo",
                line=dict(color="#22c55e", width=2),
                fill="tozeroy", fillcolor="rgba(34,197,94,0.1)"), row=1, col=1)
            colores_sub = {"ganancia":"#22c55e","flete_inicial":"#ef4444","retiro":"#8b5cf6",
                           "devolucion":"#f97316","transferencia":"#06b6d4","admin":"#6b7280","otro":"#d1d5db"}
            for sub, color in colores_sub.items():
                mask = df_chart["subtipo"] == sub
                if mask.any():
                    fig_c.add_trace(go.Bar(x=df_chart[mask]["fecha_dt2"],
                                          y=df_chart[mask]["monto_signed"],
                                          name=sub.replace("_"," ").title(),
                                          marker_color=color), row=2, col=1)
            fig_c.update_layout(height=500, showlegend=True,
                                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                                margin=dict(t=40, b=20))
            st.plotly_chart(fig_c, use_container_width=True)

        st.divider()
        st.subheader("Movimientos")
        st.caption("📋 **Historial completo** — Cada fila es una entrada o salida de dinero en tu cartera Dropi. "
                   "Usa los filtros para buscar una orden específica o ver solo las ganancias o los retiros.")
        tcol1, tcol2, tcol3 = st.columns([1,1,2])
        tipo_f    = tcol1.selectbox("Tipo", ["Todos"]+sorted(cartera_df["tipo"].dropna().unique().tolist()), key="cart_tipo2")
        subtipo_f = tcol2.selectbox("Subtipo", ["Todos"]+sorted(cartera_df["subtipo"].dropna().unique().tolist()), key="cart_sub2")
        cart_s    = tcol3.text_input("Buscar", key="cart_srch2")
        df_cs = cartera_df.copy()
        if tipo_f != "Todos": df_cs = df_cs[df_cs["tipo"] == tipo_f]
        if subtipo_f != "Todos": df_cs = df_cs[df_cs["subtipo"] == subtipo_f]
        if cart_s:
            df_cs = df_cs[df_cs["descripcion"].str.contains(cart_s, case=False, na=False) |
                          df_cs["orden_id"].str.contains(cart_s, case=False, na=False)]
        st.dataframe(df_cs[["fecha","tipo","subtipo","monto","monto_previo","orden_id","descripcion"]].rename(columns={
            "fecha":"Fecha","tipo":"Tipo","subtipo":"Subtipo","monto":"Monto (MXN)",
            "monto_previo":"Saldo previo","orden_id":"Orden ID","descripcion":"Descripción"}),
            hide_index=True, use_container_width=True)
        st.caption(f"{len(df_cs)} de {len(cartera_df)} movimientos")


# ═══════════════════════════════════════════════════════════════════════════
# TAB 9 — ÓRDENES
# ═══════════════════════════════════════════════════════════════════════════
with tab_ordenes:
    st.markdown("## 🗂 Órdenes")
    st.caption("Lista completa de todos tus pedidos. Busca una orden específica, filtra por estado o transportadora, "
               "y descubre qué pasó con cada paquete.")
    st.caption("Estados: ENTREGADO ✅ = cobrado | PENDIENTE CONFIRMACION ⏳ = en camino | CANCELADO ❌ = perdido | GUIA_GENERADA 📦 = salió pero sin confirmar")
    if st.session_state.show_pruebas:
        st.warning("🟡 Incluyendo órdenes de prueba")

    ord_show = orders.copy()

    # Añadir etiqueta visual PERDIDA al estatus
    if "es_perdido" in ord_show.columns:
        ord_show["estatus_display"] = ord_show.apply(
            lambda r: f"⚰️ PERDIDA ({DIAS_PEDIDO_PERDIDO}d+)" if r["es_perdido"] == 1 else r["estatus"], axis=1)
    else:
        ord_show["estatus_display"] = ord_show["estatus"]

    oc0, oc1, oc2, oc3, oc4 = st.columns([1,1,1,1,2])
    # Opción rápida para ver solo perdidas
    solo_perdidas = oc0.checkbox("⚰️ Solo perdidas", key="ord_solo_perdidas")
    est_opts  = ["Todos"] + sorted(ord_show["estatus"].dropna().unique().tolist())
    carr_opts = ["Todos"] + sorted(ord_show["transportadora"].dropna().unique().tolist())
    cat_opts  = ["Todos"] + sorted(ord_show["categorias"].dropna().unique().tolist()) if "categorias" in ord_show.columns else ["Todos"]

    est_sel2  = oc1.selectbox("Estatus", est_opts, key="ord_est2")
    carr_sel2 = oc2.selectbox("Carrier", carr_opts, key="ord_carr2")
    cat_sel2  = oc3.selectbox("Categoría", cat_opts[:20], key="ord_cat2")
    srch      = oc4.text_input("Buscar cliente / novedad / ciudad", key="ord_srch2")

    if solo_perdidas and "es_perdido" in ord_show.columns:
        ord_show = ord_show[ord_show["es_perdido"] == 1]
    elif est_sel2 != "Todos":
        ord_show = ord_show[ord_show["estatus"] == est_sel2]
    if carr_sel2 != "Todos":  ord_show = ord_show[ord_show["transportadora"] == carr_sel2]
    if cat_sel2 != "Todos":   ord_show = ord_show[ord_show["categorias"] == cat_sel2]
    if srch:
        mask_o = (ord_show["novedad"].str.contains(srch, case=False, na=False) |
                  ord_show["ciudad_destino"].str.contains(srch, case=False, na=False))
        if "cliente" in ord_show.columns:
            mask_o = mask_o | ord_show["cliente"].str.contains(srch, case=False, na=False)
        ord_show = ord_show[mask_o]

    # Resumen de PERDIDAS
    if "es_perdido" in orders.columns:
        n_perd_tab = int((orders["es_perdido"] == 1).sum())
        if n_perd_tab > 0:
            rev_perd = float(orders[orders["es_perdido"] == 1]["valor_venta"].sum())
            st.markdown(
                f'<div style="background:#fee2e2;border:1px solid #fca5a5;border-radius:8px;'
                f'padding:10px 16px;margin-bottom:10px">'
                f'⚰️ <b>{n_perd_tab} órdenes clasificadas como PERDIDAS</b> '
                f'(sin confirmar >{DIAS_PEDIDO_PERDIDO} días) · '
                f'<b>${rev_perd:,.0f} MXN</b> que ya no se cobrará. '
                f'Actíva "Solo perdidas" para verlas.</div>',
                unsafe_allow_html=True)

    PAGE = 50
    total_r = len(ord_show)
    total_p = max((total_r - 1) // PAGE + 1, 1)
    pg_c, _, info_c = st.columns([1,3,2])
    page = pg_c.number_input("Página", 1, total_p, 1, key="ord_pg2")
    info_c.markdown(f"<div style='text-align:right;padding-top:8px;font-size:.85em'>"
                    f"{total_r} órdenes · p{page}/{total_p}</div>", unsafe_allow_html=True)
    start = (page - 1) * PAGE
    page_df = ord_show.iloc[start:start + PAGE]

    show_cols = [c for c in ["orden_id","fecha","cliente","estatus_display","valor_venta","costo_producto",
                             "flete","transportadora","ciudad_destino","departamento",
                             "categorias","novedad","es_prueba"] if c in page_df.columns]

    # Colorear filas PERDIDAS en rojo claro
    def _color_perdidas(row):
        if "es_perdido" in ord_show.columns:
            idx = row.name
            if idx in ord_show.index and ord_show.loc[idx, "es_perdido"] == 1:
                return ["background-color:#fee2e2"] * len(row)
        return [""] * len(row)

    st.caption("📋 **Lista de pedidos** — Cada fila es un pedido. Las filas en rojo claro son órdenes clasificadas como PERDIDAS "
               "(más de 45 días sin resolverse). La columna 'Estatus' muestra el último estado en Dropi.")
    st.dataframe(page_df[show_cols].rename(columns={
        "orden_id":"ID","fecha":"Fecha","cliente":"Cliente","estatus_display":"Estatus",
        "valor_venta":"Venta","costo_producto":"Costo","flete":"Flete","transportadora":"Carrier",
        "ciudad_destino":"Ciudad","departamento":"Estado","categorias":"Categorías",
        "novedad":"Novedad","es_prueba":"🧪"}),
        hide_index=True, use_container_width=True)
    st.caption(f"{total_r} órdenes | {n_pruebas_excl} pruebas excluidas del período")
