# config.py — Fuente de verdad del negocio Ojo con el Trend

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DB_PATH  = BASE_DIR / "data" / "processed" / "negocio.db"
RAW_DROPI_DIR = BASE_DIR / "data" / "raw" / "dropi"
RAW_META_DIR  = BASE_DIR / "data" / "raw" / "meta"
RAW_CARTERA_DIR = BASE_DIR / "data" / "raw" / "Cartera de Dropi"

NEGOCIO = {
    "nombre":     "Ojo con el Trend",
    "tienda":     "ojoconeltrend.com",
    "dropi_tienda": "ANTI-ESTRÉS PRO",
    "modelo":     "Dropshipping COD",
    "plataforma": "Shopify + Dropi",
    "trafico":    "Meta Ads (EUR)",
    "pais":       "México",
    "shopify_url": "https://ojoconeltrend.com",
}

TASA_EUR_MXN = float(os.getenv("TASA_EUR_MXN", 21.5))

BENCHMARKS = {
    "roas_real_min":      1.5,
    "roas_real_objetivo": 2.5,
    "mer_peligro":        2.0,
    "mer_objetivo":       3.0,
    "cancelacion_max":    15.0,
    "entrega_min":        70.0,
    "ctr_min":            2.0,
    "dias_pauta_alerta":  3,
    "aov_objetivo":       1500,
}

# Exclusión de órdenes de prueba internas
TEST_APELLIDO_EXCLUIDO = "rosales"
TEST_NOMBRES_COMPLETOS = ["laura betancourt", "valeria enrique", "valeria enriquez"]

# Concepto de cartera que = retiro a banco personal (no es gasto operativo)
CARTERA_RETIRO_KEYWORD = "SALIDA POR PETICION DE RETIRO DE SALDO EN CARTERA"

# Saldo real actual de Dropi (override manual cuando el historial esté incompleto)
SALDO_DROPI_REAL = 5060.0

# Días sin confirmar a partir de los cuales una orden pipeline se considera PERDIDA
DIAS_PEDIDO_PERDIDO = 45

# Extracción de producto desde nombre de campaña Meta (orden importa — más específico primero)
PRODUCTO_MAP = [
    # ── Activos en Shopify (SKUs reales: ojoconeltrend.com) ───────────────
    ("FLO",            "Flo Ovarian Support"),
    ("OVARIAN",        "Flo Ovarian Support"),
    ("INOSITOL",       "Inositol + Ashwagandha"),
    ("PRIMAL",         "Primal Queen"),
    ("QUEEN",          "Primal Queen"),
    ("GOLI",           "Goli Vinagre de Manzana"),
    ("VINAGRE",        "Vinagre de Manzana"),
    ("MANZANA",        "Vinagre de Manzana"),
    # ── Próximos lanzamientos (DRAFT en Shopify) ──────────────────────────
    ("MORINGA",        "Moringa"),
    ("MELENA",         "Melena de León"),
    ("TURK",           "Turkesterone"),
    ("DEBLOAT",        "Zesty Debloat"),
    ("ZESTY",          "Zesty Debloat"),
    ("CURCUMA",        "Cúrcuma"),
    ("CÚRCUMA",        "Cúrcuma"),
    ("GLUCOS",         "Glucosamina"),
    ("CARDO",          "Cardo Mariano"),
    ("BLOOM",          "Bloom"),
    # ── Suplementos generales ─────────────────────────────────────────────
    ("ASHWAGAND",      "Ashwagandha"),
    # Shilajit — dos presentaciones activas. Orden importa: más específico primero.
    # Si nombras la campaña con GOMAS o ULTRA se separan automáticamente.
    ("SHILAJIT GOMAS",  "Shilajit Gomas 60 Pcs 3000mg"),
    ("SHILAJIT ULTRA",  "FlyNew Shilajit Ultra: Ultimate Potency"),
    ("GOMAS SHILAJIT",  "Shilajit Gomas 60 Pcs 3000mg"),
    ("ULTRA SHILAJIT",  "FlyNew Shilajit Ultra: Ultimate Potency"),
    ("FLYNEW",          "FlyNew Shilajit Ultra: Ultimate Potency"),
    ("SHILAJIT",        "Shilajit (ambas presentaciones)"),
    ("MELATONIN",      "Melatonina"),
    ("MAGNESIO",       "Magnesio Citrato"),
    ("CITRATO",        "Magnesio Citrato"),
    ("NELLO",          "Nello SuperCalm"),
    ("SUPERCALM",      "Nello SuperCalm"),
    ("SUPER CALM",     "Nello SuperCalm"),
    ("COLAGEN",        "Colágeno Hidrolizado"),
    ("COLÁG",          "Colágeno Hidrolizado"),
    ("OMEGA",          "Omega 3"),
    ("VITAMINA",       "Vitaminas"),
    ("ZINC",           "Zinc"),
    ("PROBIO",         "Probiótico"),
    ("BERBERINA",      "Berberina"),
    ("CREATINA",       "Creatina"),
    ("PROTEINA",       "Proteína"),
    # ── Productos históricos (ya no activos) ─────────────────────────────
    ("CURVAFIT",       "Curvafit ⚠️"),
    ("SECALISO",       "SecaLiso"),
    ("SECA LISO",      "SecaLiso"),
    ("SERUM REAL",     "Serum Real"),
    ("SERUM",          "Serum Real"),
    ("VUELTA",         "Vuelta Fácil"),
    ("FRIOWALM",       "FrioCalm"),
    ("FRIOCALM",       "FrioCalm"),
    ("SONRISA",        "Sonrisa Express"),
    ("CALENTAMIENTO",  "Calentamiento ToF"),
]

def producto_desde_campana(nombre_campana: str) -> str:
    n = str(nombre_campana).upper()
    for key, prod in PRODUCTO_MAP:
        if key in n:
            return prod
    return "Otro"

# Estados de devolución en Dropi
DEV_ESTADOS = [
    "PAQUETE EN DEVOLUCION",
    "DEVOLUCION",
    "DEVOLUCION EN PROCESO",
    "EN CIUDAD DE DESTINO DEVOLUCIÓN",
    "DEVOLUCION EN TRANSITO",
    "EN CAMINO A CIUDAD DE ORIGEN DEVOLUCIÓN",
    "PARA DEVOLUCIÓN",
]

# Estados que significan "en tránsito / pipeline"
PIPELINE_EXCLUIDOS = {"CANCELADO", "ENTREGADO", "RECHAZADO"} | set(DEV_ESTADOS)

DROPI_COLS = {
    "venta":          "VALOR DE COMPRA EN PRODUCTOS",
    "costo_prod":     "TOTAL EN PRECIOS DE PROVEEDOR",
    "flete":          "PRECIO FLETE",
    "devol_flete":    "COSTO DEVOLUCION FLETE",
    "ganancia":       "GANANCIA",
    "estatus":        "ESTATUS",
    "cliente":        "NOMBRE CLIENTE",
    "transportadora": "TRANSPORTADORA",
    "novedad":        "NOVEDAD",
    "fecha":          "FECHA",
    "tienda":         "TIENDA",
    "id":             "ID",
    # Nuevos campos ricos
    "ciudad":         "CIUDAD DESTINO",
    "departamento":   "DEPARTAMENTO DESTINO",
    "categorias":     "CATEGORÍAS",
    "tipo_envio":     "TIPO DE ENVIO",
    "guia":           "NÚMERO GUIA",
    "valor_facturado":"VALOR FACTURADO",
}

META_COLS = {
    "gasto":          "Importe gastado (EUR)",
    "compras":        "Compras",
    "roas":           "ROAS de compras",
    "cpa":            "Costo por compra",
    "clics":          "Clics en el enlace",
    "impresiones":    "Impresiones",
    "ctr":            "CTR (porcentaje de clics en el enlace)",
    "cpc":            "CPC (costo por clic en el enlace)",
    "carritos":       "Artículos agregados al carrito",
    "valor_conv":     "Valor de conversión de compras",
    "dia":            "Día",
    "campana":        "Nombre de la campaña",
    "anuncio":        "Nombre del anuncio",
    # Nuevos
    "adset":          "Nombre del conjunto de anuncios",
    "alcance":        "Alcance",
    "frecuencia":     "Frecuencia",
    "cpm":            "CPM (costo por mil impresiones)",
    "video_25":       "Reproducciones de video hasta el 25%",
    "video_50":       "Reproducciones de video hasta el 50%",
    "avg_video":      "Tiempo promedio de reproducción del video",
    "clics_todos":    "Clics (todos)",
    "ctr_todos":      "CTR (todos)",
}

CONTEXTO_HISTORICO = {
    "primer_reporte":    "2026-05-04",
    "tienda_activa":     "ojoconeltrend.com (Shopify Basic, MXN, México)",
    "dropi_tienda":      "ANTI-ESTRÉS PRO",
    "problema_conocido": "brecha de atribución Meta vs Dropi (ventana 7d + view-through)",
    "shopify_activos": [
        "Shilajit Gomas 60 Pcs — $899 — SKU: SHILAJIT-GOMAS",
        "Flo Ovarian Support — $999 — SKU: FLO-OVARIAN",
        "Inositol + Ashwagandha + Vit D — $999 — SKU: SUPLEMENTO-INOSITOL",
        "Primal Queen Bienestar Mujer — $1,099 — SKU: SUPLEMENTO-PRIMAL-QUEEN",
        "Goli Vinagre de Manzana Gomas — $899 — SKU: SUPLEMENTO-GOLI-VINAGRE",
    ],
    "shopify_drafts_proximos": [
        "Moringa 60 caps — $599", "Melena de León 180 caps — $799",
        "Shilajit + Maca 100 caps — $899", "Turkesterone 120 caps — $899",
        "Glucosamina Condroitina MSM — $899", "Zesty Debloat — $799",
        "Cúrcuma 150 caps — $899", "Colágeno Hidrolizado — $899",
        "Cardo Mariano — $899", "Bloom Fresa Kiwi — $899",
    ],
    "notas": [
        "Tasa de cancelación real 9.4% sin pruebas internas",
        "QUALITY-POST tiene 30% de incidencias — monitorear",
        "Pixel de Meta sin eventos de carrito — instalar CAPI urgente",
        "Curvafit discontinuado — calidad + riesgo cuenta Meta",
        "Pivote completo a suplementos desde mayo 2026",
    ],
}
