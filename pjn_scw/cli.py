#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════════════════
  SCW – Herramienta unificada para expedientes del Poder Judicial
         Descargador · Unificador
══════════════════════════════════════════════════════════════════
Requisitos:
    pip install -r requirements.txt
    playwright install chromium

Para compresión (opcional):
    Windows : https://www.ghostscript.com/download/gsdnld.html
    Mac     : brew install ghostscript
    Linux   : sudo apt install ghostscript

Uso (desde la raíz del repositorio):
    python -m pjn_scw.cli                 ← menú interactivo
    python -m pjn_scw.cli descargar     ← descargar PDFs
    python -m pjn_scw.cli comprimir     ← comprimir PDFs (Ghostscript)
    python -m pjn_scw.cli unir          ← unir PDFs (separadores + filtro por fechas opcional)
    python -m pjn_scw.cli todo          ← descargar → comprimir → unir
    python -m pjn_scw.cli todo --dry-run
    python -m pjn_scw.cli descargar --url "https://scw.pjn.gov.ar/..."
    python -m pjn_scw.cli comprimir --calidad ebook --workers 8
    python -m pjn_scw.cli unir --desde 01/01/2023 --hasta 31/12/2024
    python -m pjn_scw.cli estado        ← resumen de archivos existentes
"""

# ── Imports estándar ──────────────────────────────────────────────────────────
import argparse
import asyncio
import concurrent.futures
import configparser
import io
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# Bootstrap de importación:
# Permite ejecutar `pjn_scw/cli.py` aun si el paquete no está instalado y el
# directorio actual no es la raíz del repo (Windows: doble click / "Run Python File").
# No afecta a PyInstaller (`sys.frozen`) ni a ejecuciones normales con `python -m ...`.
if __name__ == "__main__" and not getattr(sys, "frozen", False):
    try:
        _pkg_dir = Path(__file__).resolve().parent  # .../pjn_scw
        _repo_root = _pkg_dir.parent                # .../
        if str(_repo_root) not in sys.path:
            sys.path.insert(0, str(_repo_root))
    except Exception:
        pass

# ── Imports opcionales (se verifican al usar) ─────────────────────────────────
try:
    from pypdf import PdfReader, PdfWriter
    _PYPDF_OK = True
except ImportError:
    _PYPDF_OK = False

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as rl_canvas
    _REPORTLAB_OK = True
except ImportError:
    _REPORTLAB_OK = False

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    _PLAYWRIGHT_OK = True
except ImportError:
    _PLAYWRIGHT_OK = False

try:
    import httpx
    _HTTPX_OK = True
except ImportError:
    _HTTPX_OK = False


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════════════

def project_base() -> Path:
    """Directorio de `config.ini` y `scw.log`: raíz del repo en desarrollo; `_MEIPASS` si PyInstaller."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def _leer_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    ruta = project_base() / "config.ini"
    if ruta.exists():
        cfg.read(ruta, encoding="utf-8")
    return cfg

_cfg = _leer_config()

def _cs(s, k, fb):  return _cfg.get(s, k, fallback=fb).strip()
def _ci(s, k, fb):
    try:    return int(_cfg.get(s, k, fallback=str(fb)))
    except: return fb
def _cf(s, k, fb):
    try:    return float(_cfg.get(s, k, fallback=str(fb)))
    except: return fb
def _cb(s, k, fb):
    try:    return _cfg.getboolean(s, k, fallback=fb)
    except: return fb

# Valores efectivos (config.ini > hardcoded)
CFG = {
    # descarga
    "url_default":       _cs("descarga", "url_default",      "https://scw.pjn.gov.ar/scw/expediente.seam?cid=682804"),
    "carpeta_base":      Path(_cs("descarga", "carpeta_base",     "expedientes")),
    "carpeta_pdfs":      Path(_cs("descarga", "carpeta_salida",   "expediente_pdfs")),
    "max_concurrentes":  _ci("descarga", "max_concurrentes",  8),
    "pausa_pagina":      _cf("descarga", "pausa_pagina",      4.0),
    "timeout_tabla":     _ci("descarga", "timeout_tabla",     120),
    "timeout_ajax":      _ci("descarga", "timeout_ajax",      30),
    "reintentos":        _ci("descarga", "reintentos",        3),
    "max_paginas_scraping": _ci("descarga", "max_paginas_scraping", 500),
    # union
    "archivo_unificado": Path(_cs("union", "archivo_salida",  "expediente_unificado.pdf")),
    "calidad":           _cs("union", "calidad",              "ebook"),
    "workers":           _ci("union", "workers",              max(1, os.cpu_count() // 2)),
    "sin_comprimir":     _cb("union", "sin_comprimir",        False),
    "unir_desde":        _cs("union", "desde",                ""),
    "unir_hasta":        _cs("union", "hasta",                ""),
}


def _path_cfg(path_like: Path | str) -> Path:
    """
    Rutas relativas del config.ini: si existe bajo la raíz del proyecto
    (carpeta del config), usar esa; si no, respecto del cwd.
    Rutas absolutas se devuelven sin cambiar.
    """
    p = Path(path_like)
    if p.is_absolute():
        return p
    en_proyecto = (project_base() / p).resolve()
    if en_proyecto.exists():
        return en_proyecto
    return (Path.cwd() / p).resolve()


# Constantes de diseño
COLOR_AZUL = (0.12, 0.31, 0.58)
COLOR_NEG  = (0.1, 0.1, 0.1)


# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def _setup_logging():
    log_path = project_base() / "scw.log"
    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Silenciar pypdf
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    return logging.getLogger("scw")

log = _setup_logging()

# Última carpeta de descarga exitosa (para encadenar comprimir/unir en `todo`).
ULTIMA_CARPETA_DESCARGA: Path | None = None


# ══════════════════════════════════════════════════════════════════════════════
#  UTILIDADES COMPARTIDAS
# ══════════════════════════════════════════════════════════════════════════════

def _mb(ruta: Path) -> str:
    mb = ruta.stat().st_size / 1_048_576
    return f"{mb:.1f} MB" if mb < 1000 else f"{mb/1024:.2f} GB"

def _barra(hecho: int, total: int, ancho: int = 30) -> str:
    lleno = ancho * hecho // total if total else 0
    return "█" * lleno + "░" * (ancho - lleno)

def _verificar_deps(deps: list[tuple[bool, str, str]]):
    """deps: [(disponible, nombre_paquete, pip_install)]"""
    faltantes = [(n, p) for ok, n, p in deps if not ok]
    if faltantes:
        log.error("Dependencias faltantes:")
        for nombre, pip in faltantes:
            log.error("  %s  →  pip install %s", nombre, pip)
        sys.exit(1)

def _notificar(titulo: str, mensaje: str):
    """Notificación de escritorio multiplataforma (sin deps extra)."""
    sistema = platform.system()
    try:
        if sistema == "Darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{mensaje}" with title "{titulo}"'],
                check=False, capture_output=True
            )
        elif sistema == "Linux":
            if shutil.which("notify-send"):
                subprocess.run(["notify-send", titulo, mensaje],
                               check=False, capture_output=True)
        elif sistema == "Windows":
            # PowerShell toast (no requiere nada extra en Win10+)
            ps = (
                f'[Windows.UI.Notifications.ToastNotificationManager, '
                f'Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;'
                f'$t = [Windows.UI.Notifications.ToastTemplateType]::ToastText02;'
                f'$xml = [Windows.UI.Notifications.ToastNotificationManager]'
                f'::GetTemplateContent($t);'
                f'$xml.GetElementsByTagName("text")[0].AppendChild('
                f'$xml.CreateTextNode("{titulo}")) | Out-Null;'
                f'$xml.GetElementsByTagName("text")[1].AppendChild('
                f'$xml.CreateTextNode("{mensaje}")) | Out-Null;'
                f'$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);'
                f'[Windows.UI.Notifications.ToastNotificationManager]'
                f'::CreateToastNotifier("SCW").Show($toast);'
            )
            subprocess.run(["powershell", "-Command", ps],
                           check=False, capture_output=True)
    except Exception:
        pass  # Notificaciones son opcionales

def _sonido_fin():
    """Beep/sonido al terminar (sin deps extra)."""
    sistema = platform.system()
    try:
        if sistema == "Windows":
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        elif sistema == "Darwin":
            subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"],
                           check=False, capture_output=True)
        elif sistema == "Linux":
            for cmd in [["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"],
                        ["aplay", "/usr/share/sounds/alsa/Front_Center.wav"]]:
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, check=False, capture_output=True)
                    break
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  COMANDO: ESTADO
# ══════════════════════════════════════════════════════════════════════════════

def cmd_estado(_args):
    """Muestra un resumen de los archivos existentes en el proyecto."""
    sep = "─" * 58

    print(f"\n{'═'*58}")
    print("  Estado del proyecto SCW")
    print(f"{'═'*58}")

    # PDFs descargados
    carpeta_pdfs = _path_cfg(CFG["carpeta_pdfs"])
    if carpeta_pdfs.exists():
        pdfs = sorted(carpeta_pdfs.glob("*.pdf"))
        errores = carpeta_pdfs / "_errores.txt"
        tam = sum(p.stat().st_size for p in pdfs) / 1_048_576
        print(f"\n  📥 PDFs descargados  ({carpeta_pdfs})")
        print(f"     Archivos  : {len(pdfs)}")
        print(f"     Tamaño    : {tam:.1f} MB")
        if errores.exists():
            n_err = len(errores.read_text(encoding="utf-8").strip().splitlines())
            print(f"     ⚠ Errores : {n_err} (ver _errores.txt)")
    else:
        print(f"\n  📥 PDFs descargados  — carpeta no creada aún")

    # PDF unificado
    unificado = _path_cfg(CFG["archivo_unificado"])
    print(f"\n  📄 PDF unificado  ({unificado})")
    if unificado.exists():
        reader = None
        try:
            reader = PdfReader(str(unificado), strict=False)
            npags = len(reader.pages)
        except Exception:
            npags = "?"
        print(f"     Existe    : sí")
        print(f"     Tamaño    : {_mb(unificado)}")
        print(f"     Páginas   : {npags}")
    else:
        print(f"     Existe    : no")

    # Config activa
    print(f"\n  ⚙  Configuración activa (config.ini)")
    print(f"     URL          : {CFG['url_default']}")
    print(f"     Concurrentes : {CFG['max_concurrentes']}")
    print(f"     Calidad GS   : {CFG['calidad']}")
    print(f"     Workers GS   : {CFG['workers']}")
    print(f"\n{'═'*58}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  REGISTRO DE EXPEDIENTES
# ══════════════════════════════════════════════════════════════════════════════

NOMBRE_REGISTRO = "_registro.json"


def _nombre_carpeta_seguro(texto: str, max_len: int = 80) -> str:
    """Convierte el nombre del expediente en un nombre de carpeta válido."""
    texto = re.sub(r'[\\/*?:"<>|\n\r\t]', ' ', texto)
    texto = re.sub(r'\s+', ' ', texto).strip()
    # Colapsar múltiples guiones/puntos
    texto = re.sub(r'[.\-]{2,}', '-', texto)
    return texto[:max_len].strip('. ')


async def _extraer_nombre_expediente(page) -> str:
    """
    Intenta obtener la carátula/nombre del expediente desde la página.
    Prueba varios selectores comunes del sitio SCW y cae en el CID si no encuentra.
    """
    selectores = [
        # Carátula en el encabezado del expediente
        'span[id*="caratula"]',
        'td[id*="caratula"]',
        '.caratula',
        # Título de sección
        'h1', 'h2',
        # Encabezado de panel
        '[id*="expediente"] .rf-p-hdr',
        '[id*="titulo"]',
    ]
    for sel in selectores:
        try:
            el = await page.query_selector(sel)
            if el:
                texto = (await el.inner_text()).strip()
                if texto and len(texto) > 4:
                    return _nombre_carpeta_seguro(texto)
        except Exception:
            pass

    # Fallback: usar el título de la pestaña
    # Títulos genéricos del sitio que NO sirven como nombre de carpeta
    _TITULOS_GENERICOS = {
        "sistema de consulta web poder judicial de la nación",
        "sistema de consulta web poder judicial de la nacion",
        "consulta web poder judicial",
        "poder judicial de la nación",
        "poder judicial de la nacion",
        "scw",
        "expediente",
        "",
    }
    try:
        titulo = await page.title()
        if titulo:
            clean = re.sub(r'(?i)scw\s*[–\-|]?\s*', '', titulo).strip()
            if clean and clean.lower() not in _TITULOS_GENERICOS:
                return _nombre_carpeta_seguro(clean)
    except Exception:
        pass

    # Último recurso: usar el CID de la URL
    try:
        url_actual = page.url
        m = re.search(r'cid=(\d+)', url_actual)
        if m:
            return f"expediente_cid_{m.group(1)}"
    except Exception:
        pass

    return "expediente_sin_nombre"


def _cargar_registro(carpeta: Path) -> dict:
    """Carga el registro JSON de la carpeta. Devuelve dict vacío si no existe."""
    ruta = carpeta / NOMBRE_REGISTRO
    if ruta.exists():
        try:
            return json.loads(ruta.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"documentos": {}}


def _guardar_registro(carpeta: Path, data: dict):
    """Guarda el registro JSON en la carpeta."""
    ruta = carpeta / NOMBRE_REGISTRO
    ruta.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _expediente_desde_carpeta(sub: Path) -> dict | None:
    """Construye el dict de listado si `sub` tiene _registro.json; si no, None."""
    reg_path = sub / NOMBRE_REGISTRO
    if not sub.is_dir() or not reg_path.is_file():
        return None
    try:
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    docs      = reg.get("documentos", {})
    pdfs      = list(sub.glob("*.pdf"))
    tam_bytes = sum(p.stat().st_size for p in pdfs)

    return {
        "nombre":       reg.get("nombre", sub.name),
        "carpeta":      sub.resolve(),
        "url":          reg.get("url", ""),
        "cid":          reg.get("cid", ""),
        "jurisdiccion": reg.get("jurisdiccion", ""),
        "numero":       reg.get("numero", ""),
        "anio":         reg.get("anio", ""),
        "total":        reg.get("total_documentos", len(docs)),
        "descargados":  len([d for d in docs.values() if d.get("descargado_en")]),
        "errores":      len([d for d in docs.values() if d.get("error")]),
        "ultima_act":   reg.get("ultima_actualizacion", ""),
        "tam_mb":       tam_bytes / 1_048_576,
    }


def listar_expedientes(en_carpeta: Path | None = None) -> list[dict]:
    """
    Escanea expedientes registrados (carpetas con _registro.json):
    - Subcarpetas de ``carpeta_base`` (p.ej. expedientes/caso1/).
    - La carpeta ``carpeta_salida`` del config si tiene _registro.json en la raíz
      (descargas directas a expediente_pdfs/).
    - Subcarpetas de ``carpeta_salida`` que tengan registro.
    """
    base_default = en_carpeta or CFG["carpeta_base"]
    base = _path_cfg(base_default)
    salida = _path_cfg(CFG["carpeta_pdfs"])

    resultado: list[dict] = []
    vistos: set[Path] = set()

    def agregar(sub: Path) -> None:
        item = _expediente_desde_carpeta(sub)
        if not item:
            return
        clave = item["carpeta"]
        if clave in vistos:
            return
        vistos.add(clave)
        resultado.append(item)

    if base.is_dir():
        for sub in sorted(base.iterdir()):
            agregar(sub)

    if salida.is_dir() and salida.resolve() != base.resolve():
        if (salida / NOMBRE_REGISTRO).is_file():
            agregar(salida)
        for sub in sorted(salida.iterdir()):
            if sub.is_dir():
                agregar(sub)

    resultado.sort(key=lambda e: e["nombre"].lower())
    return resultado


# ══════════════════════════════════════════════════════════════════════════════
#  COMANDO: DESCARGAR
# ══════════════════════════════════════════════════════════════════════════════

def _sanitizar(s: str, max_len: int = 120) -> str:
    s = re.sub(r'[\\/*?:"<>|\n\r\t]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:max_len]

def _parsear_fecha(s: str) -> str:
    s = s.strip()
    m = re.match(r'^(\d{1,2})/(\d{2})/(\d{4})$', s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo}-{int(d):02d}"
    return s.replace('/', '-')

def _fecha_a_iso(s: str) -> str | None:
    """
    Convierte cualquier formato de fecha a 'AAAA-MM-DD' para comparación.
    Acepta: DD/MM/AAAA, DD-MM-AAAA, AAAA-MM-DD, AAAA-MM, AAAA
    Devuelve None si no puede parsear.
    """
    s = s.strip()
    # Ya en ISO completo
    if re.match(r'^\d{4}-\d{2}-\d{2}$', s):
        return s
    # AAAA-MM  → primer día del mes
    if re.match(r'^\d{4}-\d{2}$', s):
        return f"{s}-01"
    # AAAA → 01/01/AAAA
    if re.match(r'^\d{4}$', s):
        return f"{s}-01-01"
    # DD/MM/AAAA o DD-MM-AAAA
    m = re.match(r'^(\d{1,2})[/\-](\d{2})[/\-](\d{4})$', s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo}-{int(d):02d}"
    return None

def _hasta_iso(s: str) -> str | None:
    """
    Como _fecha_a_iso pero para el límite superior:
    AAAA → 31/12/AAAA, AAAA-MM → último día del mes.
    """
    s = s.strip()
    if re.match(r'^\d{4}$', s):
        return f"{s}-12-31"
    if re.match(r'^\d{4}-\d{2}$', s):
        # Último día del mes aproximado (31 siempre compara bien para >= comparaciones de strings)
        return f"{s}-31"
    return _fecha_a_iso(s)

def _en_rango_fecha(fecha_iso: str, desde_iso: str | None, hasta_iso: str | None) -> bool:
    """Compara fechas en formato AAAA-MM-DD como strings (ISO ordena bien)."""
    if desde_iso and fecha_iso < desde_iso:
        return False
    if hasta_iso and fecha_iso > hasta_iso:
        return False
    return True

def _construir_nombre(fila: dict, conteo: dict) -> str:
    fecha   = _parsear_fecha(fila['fecha'])
    tipo    = _sanitizar(fila['tipo'])
    detalle = _sanitizar(fila['detalle'])
    base    = f"{fecha} - {tipo} - {detalle}" if detalle else f"{fecha} - {tipo}"
    base    = base[:180]
    conteo[base] = conteo.get(base, 0) + 1
    return f"{base} ({conteo[base]})" if conteo[base] > 1 else base

async def _extraer_filas(page) -> list:
    # Un .btn-group por acciones ≈ un PDF; querySelector por fila solo tomaba el 1.er link.
    return await page.evaluate('''() => {
        const base = "https://scw.pjn.gov.ar";
        const tbody = document.querySelector("#expediente\\\\:action-table tbody");
        if (!tbody) return [];

        const metaFromTr = (tr) => {
            const tdFecha = tr.querySelector("td:nth-child(3)");
            const tdTipo  = tr.querySelector("td:nth-child(4)");
            const tdDet   = tr.querySelector("td:nth-child(5)");
            const fechaEl = tdFecha?.querySelector("span.font-color-black") || tdFecha;
            const tipoEl  = tdTipo?.querySelector("span.font-color-black")  || tdTipo;
            const detEl   = tdDet?.querySelector("span.font-color-black")   || tdDet;
            const fecha   = (fechaEl?.innerText || "").trim();
            const tipo    = (tipoEl?.innerText  || "").trim();
            const detalle = (detEl?.innerText || "").trim();
            return { fecha, tipo, detalle };
        };

        const findPdfA = (root) => {
            return root.querySelector('a[href*="download=true"]')
                || root.querySelector('a[href*="viewer.seam"]');
        };

        const pushIfPdf = (out, tr, a) => {
            if (!a) return;
            let href = (a.getAttribute("href") || "").trim();
            if (!href || !href.includes("viewer.seam")) return;
            if (href.startsWith("/")) href = base + href;
            const { fecha, tipo, detalle } = metaFromTr(tr);
            // No filtrar por fecha/tipo: algunas páginas tardan en poblar esas celdas.
            out.push({ fecha, tipo, detalle, url: href });
        };

        const out = [];
        const groups = tbody.querySelectorAll(".btn-group");
        if (groups.length > 0) {
            for (const bg of groups) {
                const tr = bg.closest("tr");
                if (!tr) continue;
                pushIfPdf(out, tr, findPdfA(bg));
            }
        } else {
            for (const row of tbody.querySelectorAll("tr")) {
                pushIfPdf(out, row, findPdfA(row));
            }
        }
        return out;
    }''')


async def _auditar_btn_group_vs_extraccion(page, n_extr: int, label: str) -> None:
    """Compara cantidad de enlaces PDF vía .btn-group (o filas) con lo extraído."""
    try:
        data = await page.evaluate('''() => {
            const tbody = document.querySelector(
                "#expediente\\\\:action-table tbody"
            );
            if (!tbody) return null;
            const groups = tbody.querySelectorAll(".btn-group");
            const metaOk = (tr) => {
                if (!tr) return false;
                const tdFecha = tr.querySelector("td:nth-child(3)");
                const tdTipo  = tr.querySelector("td:nth-child(4)");
                const fechaEl = tdFecha?.querySelector("span.font-color-black") || tdFecha;
                const tipoEl  = tdTipo?.querySelector("span.font-color-black")  || tdTipo;
                const f = (fechaEl?.innerText || "").trim();
                const t = (tipoEl?.innerText || "").trim();
                return !!(f && t);
            };
            let linksPdf = 0;
            let conMeta = 0;
            const pickA = (root) =>
                root.querySelector('a[href*="download=true"]')
                || root.querySelector('a[href*="viewer.seam"]');
            if (groups.length > 0) {
                for (const bg of groups) {
                    const a = pickA(bg);
                    const h = a ? (a.getAttribute("href") || "").trim() : "";
                    if (!h || !h.includes("viewer.seam")) continue;
                    linksPdf++;
                    const tr = bg.closest("tr");
                    if (metaOk(tr)) conMeta++;
                }
                return {
                    modo: "btn-group",
                    linksPdf,
                    conMeta,
                    grupos: groups.length,
                };
            }
            for (const row of tbody.querySelectorAll("tr")) {
                const a = pickA(row);
                const h = a ? (a.getAttribute("href") || "").trim() : "";
                if (!h || !h.includes("viewer.seam")) continue;
                linksPdf++;
                if (metaOk(row)) conMeta++;
            }
            return { modo: "tr", linksPdf, conMeta, grupos: 0 };
        }''')
    except Exception:
        return
    if not data:
        return
    modo = data.get("modo", "")
    links_pdf = int(data.get("linksPdf", 0))
    con_meta = int(data.get("conMeta", 0))
    if links_pdf != n_extr:
        log.warning(
            "Página %s: en DOM hay %d enlace(s) PDF (%s); "
            "se extrajeron %d fila(s). Con fecha+tipo: %d.",
            label,
            links_pdf,
            modo,
            n_extr,
            con_meta,
        )


async def _firma_vista_tabla(page) -> str:
    """Identifica la vista actual de la tabla + página activa (anti-bucle en paginación)."""
    try:
        return await page.evaluate('''() => {
            const hrefsEnOrden = () => {
                const base = "https://scw.pjn.gov.ar";
                const tbody = document.querySelector(
                    "#expediente\\\\:action-table tbody"
                );
                if (!tbody) return [];
                const acc = [];
                const pickA = (root) =>
                    root.querySelector('a[href*="download=true"]')
                    || root.querySelector('a[href*="viewer.seam"]');
                const push = (a) => {
                    if (!a) return;
                    let h = (a.getAttribute("href") || "").trim();
                    if (!h || !h.includes("viewer.seam")) return;
                    if (h.startsWith("/")) h = base + h;
                    acc.push(h);
                };
                const groups = tbody.querySelectorAll(".btn-group");
                if (groups.length > 0) {
                    for (const bg of groups) {
                        push(pickA(bg));
                    }
                } else {
                    for (const row of tbody.querySelectorAll("tr")) {
                        push(pickA(row));
                    }
                }
                return acc;
            };
            const hrefs = hrefsEnOrden();
            const n = hrefs.length;
            const first = n ? hrefs[0] : "";
            const last = n ? hrefs[n - 1] : "";
            const li = document.querySelector(
                '[id$=":divPagesAct"] li.active span:last-child'
            );
            const pag = li ? li.innerText.trim() : "?";
            return [n, pag, first, last].join("|");
        }''')
    except Exception:
        return ""

async def _snapshot_paginacion(page) -> dict:
    """Estado observable antes/después del click en Siguiente (varios criterios)."""
    return await page.evaluate('''() => {
        const base = "https://scw.pjn.gov.ar";
        const td = document.querySelector(
            "#expediente\\\\:action-table tbody tr:first-child td:nth-child(3)"
        );
        const fecha = td ? td.innerText.trim() : "";
        const li = document.querySelector(
            '[id$=":divPagesAct"] li.active span:last-child'
        );
        const pag = li ? li.innerText.trim() : "?";
        const tbody = document.querySelector("#expediente\\\\:action-table tbody");
        let href = "";
        let n = 0;
        if (tbody) {
            const groups = tbody.querySelectorAll(".btn-group");
            const hrefs = [];
            const pickA = (root) =>
                root.querySelector('a[href*="download=true"]')
                || root.querySelector('a[href*="viewer.seam"]');
            if (groups.length > 0) {
                for (const bg of groups) {
                    const a = pickA(bg);
                    if (!a) continue;
                    let h = (a.getAttribute("href") || "").trim();
                    if (!h || !h.includes("viewer.seam")) continue;
                    if (h.startsWith("/")) h = base + h;
                    hrefs.push(h);
                }
            } else {
                for (const row of tbody.querySelectorAll("tr")) {
                    const a = pickA(row);
                    if (!a) continue;
                    let h = (a.getAttribute("href") || "").trim();
                    if (!h || !h.includes("viewer.seam")) continue;
                    if (h.startsWith("/")) h = base + h;
                    hrefs.push(h);
                }
            }
            n = hrefs.length;
            href = hrefs.length ? hrefs[0] : "";
        }
        return { fecha, pag, href, n };
    }''')


async def _ir_siguiente_pagina(page, timeout_ajax: int) -> bool:
    siguiente = await page.query_selector('[id$=":divPagesAct"] span[title="Siguiente"]')
    if not siguiente:
        return False
    padre = await siguiente.evaluate_handle('el => el.parentElement')
    if (await padre.evaluate('el => el.tagName')).upper() != 'A':
        return False
    firma_antes = await _firma_vista_tabla(page)
    snap_antes = await _snapshot_paginacion(page)
    await padre.as_element().click()
    cambio_detectado = False
    # Varias señales: en páginas seguidas la fecha de la 1.ª fila a menudo se repite;
    # antes solo mirábamos eso y se agotaba timeout_ajax (p. ej. 30s) al pedo.
    try:
        await page.wait_for_function(
            f'''() => {{
                const base = "https://scw.pjn.gov.ar";
                const td = document.querySelector(
                    "#expediente\\\\:action-table tbody tr:first-child td:nth-child(3)"
                );
                const fechaNow = td ? td.innerText.trim() : "";
                const li = document.querySelector(
                    '[id$=":divPagesAct"] li.active span:last-child'
                );
                const pagNow = li ? li.innerText.trim() : "?";
                const tbody = document.querySelector(
                    "#expediente\\\\:action-table tbody"
                );
                let hrefNow = "";
                let n = 0;
                if (tbody) {{
                    const hrefs = [];
                    const pickA = (root) =>
                        root.querySelector('a[href*="download=true"]')
                        || root.querySelector('a[href*="viewer.seam"]');
                    const groups = tbody.querySelectorAll(".btn-group");
                    if (groups.length > 0) {{
                        for (const bg of groups) {{
                            const a = pickA(bg);
                            if (!a) continue;
                            let h = (a.getAttribute("href") || "").trim();
                            if (!h || !h.includes("viewer.seam")) continue;
                            if (h.startsWith("/")) h = base + h;
                            hrefs.push(h);
                        }}
                    }} else {{
                        for (const row of tbody.querySelectorAll("tr")) {{
                            const a = pickA(row);
                            if (!a) continue;
                            let h = (a.getAttribute("href") || "").trim();
                            if (!h || !h.includes("viewer.seam")) continue;
                            if (h.startsWith("/")) h = base + h;
                            hrefs.push(h);
                        }}
                    }}
                    n = hrefs.length;
                    hrefNow = hrefs.length ? hrefs[0] : "";
                }}
                return fechaNow !== {json.dumps(snap_antes["fecha"])}
                    || pagNow !== {json.dumps(snap_antes["pag"])}
                    || hrefNow !== {json.dumps(snap_antes["href"])}
                    || n !== {snap_antes["n"]};
            }}''',
            timeout=timeout_ajax * 1000,
            polling=150,
        )
        cambio_detectado = True
    except PWTimeout:
        log.warning(
            "Paginación: ningún criterio (página activa / 1.er enlace / #filas / fecha "
            "1.ª fila) cambió tras %ds; se revisa la firma de la tabla.",
            timeout_ajax,
        )
    # Si detectamos el cambio por AJAX no hace falta esperar un "sleep" fijo.
    if not cambio_detectado and CFG["pausa_pagina"] > 0:
        await asyncio.sleep(CFG["pausa_pagina"])
    firma_despues = await _firma_vista_tabla(page)
    # Si el enlace "Siguiente" sigue activo pero la vista no cambió, antes
    # devolvíamos True y el bucle podía colgarse o repetir la misma página.
    if firma_despues and firma_despues == firma_antes:
        log.warning(
            "Paginación: la tabla no cambió tras 'Siguiente'; se detiene el scraping."
        )
        return False
    if not cambio_detectado and firma_despues != firma_antes:
        log.info("Paginación: avance confirmado por firma de tabla tras el timeout.")
    return True

async def _obtener_pagina_actual(page) -> int:
    try:
        texto = await page.evaluate('''() => {
            const li = document.querySelector(
                '[id$=":divPagesAct"] li.active span:last-child'
            );
            return li ? li.innerText.trim() : "?";
        }''')
        return int(texto) if texto.isdigit() else -1
    except Exception:
        return -1

async def _descargar_un_pdf(semaforo, contador, lock, fila, ruta_pdf,
                             client, reintentos, dry_run, registro_docs):
    nombre = ruta_pdf.stem

    if ruta_pdf.exists():
        async with lock:
            contador["omitidos"] += 1
            done = sum(contador[k] for k in ("ok","omitidos","errores"))
            pct  = done * 100 // contador["total"]
            print(f"  [{_barra(done, contador['total'])}] {pct:3d}%"
                  f"  EXISTE  {nombre[:60]}", end="\r")
        return

    if dry_run:
        async with lock:
            contador["dry"] += 1
            done = sum(contador[k] for k in ("ok","omitidos","errores","dry"))
            pct  = done * 100 // contador["total"]
            print(f"  [{_barra(done, contador['total'])}] {pct:3d}%"
                  f"  DRY-RUN {nombre[:55]}", end="\r")
        return

    async with semaforo:
        for intento in range(1, reintentos + 1):
            try:
                r = await client.get(fila['url'])
                r.raise_for_status()
                ct = r.headers.get('content-type', '')
                if 'pdf' not in ct and len(r.content) < 100:
                    raise ValueError(f"Respuesta inesperada: {ct}")
                ruta_pdf.write_bytes(r.content)

                async with lock:
                    contador["ok"] += 1
                    done = sum(contador[k] for k in ("ok","omitidos","errores"))
                    pct  = done * 100 // contador["total"]
                    print(f"  [{_barra(done, contador['total'])}] {pct:3d}%"
                          f"  OK  {nombre[:62]}", end="\r")
                    # Actualizar registro en memoria
                    if nombre in registro_docs:
                        registro_docs[nombre]["descargado_en"] = datetime.now().isoformat(timespec="seconds")
                        registro_docs[nombre]["tamano_bytes"]  = ruta_pdf.stat().st_size
                        registro_docs[nombre].pop("error", None)

                log.info("OK  %s", nombre)
                return

            except Exception as e:
                if intento < reintentos:
                    await asyncio.sleep(2 * intento)
                else:
                    async with lock:
                        contador["errores"] += 1
                        done = sum(contador[k] for k in ("ok","omitidos","errores"))
                        print(f"\n  !! ERROR  {nombre[:55]}  ({e})")
                        if nombre in registro_docs:
                            registro_docs[nombre]["error"] = str(e)
                    log.error("ERROR  %s  %s", nombre, e)
                    log_err = ruta_pdf.parent / "_errores.txt"
                    with open(log_err, "a", encoding="utf-8") as f:
                        f.write(f"{nombre}.pdf\t{fila['url']}\t{e}\n")

async def _run_descargar(args):
    _verificar_deps([
        (_PLAYWRIGHT_OK, "playwright", "playwright"),
        (_HTTPX_OK,      "httpx",      "httpx"),
    ])

    url          = args.url or CFG["url_default"]
    concurr      = args.concurrentes or CFG["max_concurrentes"]
    reintentos   = args.reintentos or CFG["reintentos"]
    dry_run      = getattr(args, "dry_run", False)
    desde_raw    = getattr(args, "desde", None) or ""
    hasta_raw    = getattr(args, "hasta", None) or ""
    jurisdiccion = getattr(args, "jurisdiccion", None) or ""
    numero       = getattr(args, "numero", None) or ""
    anio         = getattr(args, "anio", None) or ""

    # Normalizar rango de fechas
    desde_iso = _fecha_a_iso(desde_raw) if desde_raw else None
    hasta_iso = _hasta_iso(hasta_raw)   if hasta_raw else None

    # Carpeta: si se pasa explícitamente se respeta; si no, se crea bajo carpeta_base
    carpeta_override = getattr(args, "carpeta", None)

    print(f"\n{'═'*58}")
    print(f"  PASO 1 — Descarga de PDFs")
    if dry_run:
        print("  *** MODO DRY-RUN: no se descargará nada ***")
    print(f"{'═'*58}")
    print(f"  URL     : {url}")
    if desde_iso or hasta_iso:
        print(f"  Rango   : {desde_raw or '...'} → {hasta_raw or '...'}")
    print(f"  Paralel.: {concurr}  |  Reintentos: {reintentos}\n")
    log.info("Descarga iniciada — URL: %s", url)
    if dry_run:
        log.info("Modo dry-run: tras el scraping se simularán las descargas (sin escribir PDFs).")

    todas_las_filas: list = []
    cookies_playwright: list = []
    nombre_expediente: str  = ""
    t_inicio = time.time()

    async with async_playwright() as pw:
        # Configurar Chromium bundleado si existe (modo empaquetado)
        base_exe = Path(getattr(sys, "_MEIPASS", None) or sys.executable).parent
        browsers_dir = base_exe / "browsers"
        launch_kwargs = {}
        if browsers_dir.exists():
            # Buscar el ejecutable de Chromium dentro de la carpeta bundleada
            for patron in ["chromium-*/chrome-win/chrome.exe",
                           "chromium-*/chrome-linux/chrome",
                           "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"]:
                hits = list(browsers_dir.glob(patron))
                if hits:
                    launch_kwargs["executable_path"] = str(hits[0])
                    break

        browser = await pw.chromium.launch(headless=False, **launch_kwargs)
        context = await browser.new_context(accept_downloads=False)
        page    = await context.new_page()


        # Si hay datos de búsqueda, navegar al formulario público y rellenarlo
        if jurisdiccion or numero or anio:
            url_home = "https://scw.pjn.gov.ar/scw/home.seam"
            print(f"  Abriendo formulario de búsqueda...\n")
            await page.goto(url_home, timeout=60_000)
            try:
                await page.wait_for_selector(
                    '#formPublica\\:camaraNumAni',
                    timeout=15_000,
                )
                if jurisdiccion:
                    try:
                        await page.select_option(
                            '#formPublica\\:camaraNumAni',
                            label=jurisdiccion,
                        )
                    except Exception:
                        await page.select_option(
                            '#formPublica\\:camaraNumAni',
                            value=jurisdiccion,
                        )
                    await page.wait_for_timeout(600)
                if numero:
                    await page.fill('#formPublica\\:numero', numero)
                if anio:
                    await page.fill('#formPublica\\:anio', anio)
                print("  Campos completados. Completá el CAPTCHA y hacé clic en Consultar.\n")
                # Esperar a que el usuario complete el CAPTCHA y llegue al expediente
                await page.wait_for_selector(
                    '#expediente\\:action-table tbody tr',
                    timeout=CFG["timeout_tabla"] * 1000,
                )
            except PWTimeout:
                print("  Tiempo agotado esperando. Verificá que completaste el CAPTCHA.\n")
                await browser.close()
                return
            except Exception as e:
                print(f"  Aviso: error en el formulario ({e}). Intentando URL directa.\n")
                await page.goto(url, timeout=60_000)
                await page.wait_for_selector(
                    '#expediente\\:action-table tbody tr',
                    timeout=CFG["timeout_tabla"] * 1000,
                )
        else:
            print("  Abriendo expediente... (iniciá sesión si es necesario)\n")
            await page.goto(url, timeout=60_000)
            try:
                await page.wait_for_selector(
                    '#expediente\\:action-table tbody tr',
                    timeout=CFG["timeout_tabla"] * 1000,
                )
            except PWTimeout:
                log.error("No se encontró la tabla. Verificar URL o login.")
                await browser.close()
                return


        # Extraer nombre del expediente para nombrar la carpeta
        nombre_expediente = await _extraer_nombre_expediente(page)
        print(f"  Expediente: {nombre_expediente}\n")

        conteo_nombres: dict = {}
        pagina = 1
        paginas_recorridas = 0
        ultima_fecha_conocida: str | None = None
        log.info("Scraping: recorriendo tabla de documentos (paginación)...")
        while True:
            pag_actual = await _obtener_pagina_actual(page)
            label      = str(pag_actual) if pag_actual > 0 else str(pagina)
            filas      = await _extraer_filas(page)
            # Si faltan fechas, usar la última fecha conocida (mantiene el orden).
            for fila in filas:
                f = (fila.get("fecha") or "").strip()
                if f:
                    ultima_fecha_conocida = f
                else:
                    fila["fecha"] = ultima_fecha_conocida or "sin_fecha"
                if not (fila.get("tipo") or "").strip():
                    fila["tipo"] = "SIN_TIPO"
            for fila in filas:
                fila['nombre'] = _construir_nombre(fila, conteo_nombres)
            todas_las_filas.extend(filas)
            paginas_recorridas += 1
            linea_pag = (
                f"Pág. {label:>4} | {len(filas):>3} PDFs en esta página | "
                f"acumulado: {len(todas_las_filas)}"
            )
            print(f"  {linea_pag}")
            log.info("Scraping %s", linea_pag.strip())
            await _auditar_btn_group_vs_extraccion(page, len(filas), label)
            hay_sig = await _ir_siguiente_pagina(page, CFG["timeout_ajax"])
            if not hay_sig:
                print(f"\n  Scraping completo: {len(todas_las_filas)} PDFs encontrados\n")
                log.info(
                    "Scraping terminado: %d página(s) visitada(s), %d documento(s) en lista.",
                    paginas_recorridas,
                    len(todas_las_filas),
                )
                break
            if paginas_recorridas >= CFG["max_paginas_scraping"]:
                log.warning(
                    "Scraping detenido: límite max_paginas_scraping (%d) — "
                    "subilo en [descarga] si el expediente es muy largo.",
                    CFG["max_paginas_scraping"],
                )
                print(
                    f"\n  ⚠  Límite de páginas ({CFG['max_paginas_scraping']}) alcanzado; "
                    f"{len(todas_las_filas)} PDFs en lista.\n"
                )
                break
            pagina += 1

        cookies_playwright = await context.cookies()
        await browser.close()

    if not todas_las_filas:
        log.warning("No se encontraron PDFs.")
        return

    log.info(
        "Lista de documentos lista: %d ítem(s) — expediente: %s",
        len(todas_las_filas),
        nombre_expediente,
    )

    # Filtrar por rango de fechas si se especificó
    if desde_iso or hasta_iso:
        total_antes = len(todas_las_filas)
        todas_las_filas = [
            f for f in todas_las_filas
            if _en_rango_fecha(_parsear_fecha(f['fecha']), desde_iso, hasta_iso)
        ]
        excluidos = total_antes - len(todas_las_filas)
        if excluidos:
            print(f"  Filtro de fechas: {excluidos} doc(s) fuera del rango excluidos.")
        print(f"  Documentos dentro del rango: {len(todas_las_filas)}\n")
        if not todas_las_filas:
            log.warning("Ningún documento cae dentro del rango de fechas indicado.")
            return

    # Determinar carpeta de destino
    if carpeta_override:
        carpeta = Path(carpeta_override)
    else:
        carpeta_base = _path_cfg(CFG["carpeta_base"])
        carpeta_base.mkdir(parents=True, exist_ok=True)
        carpeta = carpeta_base / nombre_expediente

    carpeta.mkdir(parents=True, exist_ok=True)

    # Cargar registro existente
    registro = _cargar_registro(carpeta)
    docs_registro = registro.setdefault("documentos", {})

    # Detectar documentos nuevos (no estaban en el registro anterior)
    nombres_nuevos = [f['nombre'] for f in todas_las_filas
                      if f['nombre'] not in docs_registro]
    if nombres_nuevos:
        print(f"  ✨ Documentos nuevos detectados: {len(nombres_nuevos)}\n")
    else:
        print(f"  Sin documentos nuevos desde la última descarga.\n")

    # Actualizar el registro con todos los docs del scraping
    for fila in todas_las_filas:
        nombre = fila['nombre']
        if nombre not in docs_registro:
            docs_registro[nombre] = {
                "url":          fila['url'],
                "fecha_doc":    _parsear_fecha(fila['fecha']),
                "tipo":         fila['tipo'],
                "detalle":      fila.get('detalle', ''),
                "descargado_en": None,
                "tamano_bytes":  None,
            }

    # Extraer CID de la URL
    cid_match = re.search(r'cid=(\d+)', url)
    cid = cid_match.group(1) if cid_match else ""

    ahora = datetime.now().isoformat(timespec="seconds")
    registro.update({
        "nombre":             nombre_expediente,
        "url":                url,
        "cid":                cid,
        "jurisdiccion":       jurisdiccion,
        "numero":             numero,
        "anio":               anio,
        "ultima_actualizacion": ahora,
        "total_documentos":   len(todas_las_filas),
    })
    registro.setdefault("primera_descarga", ahora)

    if not dry_run:
        _guardar_registro(carpeta, registro)

    print(f"  Destino : {carpeta.resolve()}\n")

    cookies_jar = {c['name']: c['value'] for c in cookies_playwright}
    semaforo    = asyncio.Semaphore(concurr)
    lock        = asyncio.Lock()
    contador    = {
        "ok": 0, "omitidos": 0, "errores": 0, "dry": 0,
        "total": len(todas_las_filas),
    }

    print(f"  Descargando {len(todas_las_filas)} PDFs ({concurr} simultáneos)...\n")
    if dry_run:
        log.info(
            "Dry-run: simulando %d descarga(s) en paralelo (la barra de progreso no se duplica en el log).",
            len(todas_las_filas),
        )

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
        ),
        'Referer': 'https://scw.pjn.gov.ar/',
    }
    limits = httpx.Limits(
        max_connections=max(10, concurr * 2),
        max_keepalive_connections=max(5, concurr),
    )
    async with httpx.AsyncClient(
        cookies=cookies_jar,
        headers=headers,
        follow_redirects=True,
        timeout=60,
        limits=limits,
    ) as client:
        tareas = [
            _descargar_un_pdf(
                semaforo, contador, lock, fila,
                carpeta / f"{fila['nombre']}.pdf",
                client, reintentos, dry_run,
                docs_registro,
            )
            for fila in todas_las_filas
        ]
        await asyncio.gather(*tareas)

    # Guardar registro actualizado con estados de descarga
    if not dry_run:
        _guardar_registro(carpeta, registro)

    elapsed = time.time() - t_inicio
    mins, segs = divmod(int(elapsed), 60)
    print()

    if dry_run:
        log.info(
            "Dry-run finalizado — simulados:%d  OK:%d  Omitidos:%d  Errores:%d  Tiempo:%dm%ds "
            "(los PDFs ya estaban en la lista del scraping; revisá las líneas “Scraping Pág.” arriba).",
            contador["dry"],
            contador["ok"],
            contador["omitidos"],
            contador["errores"],
            mins,
            segs,
        )
    else:
        log.info(
            "Descarga completa — OK:%d  Omitidos:%d  Errores:%d  Tiempo:%dm%ds",
            contador["ok"],
            contador["omitidos"],
            contador["errores"],
            mins,
            segs,
        )

    print(f"\n{'─'*58}")
    print(f"  ✓  Descargados : {contador['ok']}")
    print(f"  –  Ya existían : {contador['omitidos']}")
    if nombres_nuevos:
        print(f"  ✨ Nuevos      : {len(nombres_nuevos)}")
    if dry_run:
        print(f"  ·  Dry-run     : {contador['dry']}")
    if contador['errores']:
        print(f"  !  Errores     : {contador['errores']}  (ver _errores.txt)")
    print(f"  ⏱  Tiempo      : {mins}m {segs}s")
    print(f"  📁  Carpeta     : {carpeta.resolve()}")
    print(f"{'─'*58}\n")

    return contador, carpeta

def cmd_descargar(args):
    global ULTIMA_CARPETA_DESCARGA
    out = asyncio.run(_run_descargar(args))
    if out is not None:
        ULTIMA_CARPETA_DESCARGA = out[1].resolve()


def _carpeta_trabajo_todo(args) -> Path:
    """Tras `descargar` en el flujo `todo`, usa la misma carpeta para comprimir/unir."""
    if getattr(args, "carpeta", None):
        return _path_cfg(args.carpeta)
    if ULTIMA_CARPETA_DESCARGA is not None:
        return ULTIMA_CARPETA_DESCARGA
    return _path_cfg(CFG["carpeta_pdfs"])


# ══════════════════════════════════════════════════════════════════════════════
#  COMANDO: COMPRIMIR (Ghostscript, in-place)
# ══════════════════════════════════════════════════════════════════════════════


def cmd_comprimir(args):
    """Comprime cada PDF de la carpeta con Ghostscript y reemplaza el archivo si baja de tamaño."""
    carpeta = (
        _path_cfg(args.carpeta)
        if getattr(args, "carpeta", None)
        else _path_cfg(CFG["carpeta_pdfs"])
    )
    calidad = getattr(args, "calidad", None) or CFG["calidad"]
    workers = getattr(args, "workers", None) or CFG["workers"]
    dry_run = getattr(args, "dry_run", False)

    gs_cmd = _buscar_ghostscript()
    if not gs_cmd:
        log.error("Ghostscript no encontrado. Instalalo para comprimir PDFs.")
        sys.exit(1)

    if not carpeta.is_dir():
        log.error("La carpeta '%s' no existe o no es un directorio.", carpeta)
        sys.exit(1)

    pdfs = sorted([p for p in carpeta.glob("*.pdf") if not p.name.startswith("_")])
    if not pdfs:
        log.error("No se encontraron PDFs en '%s'.", carpeta)
        sys.exit(1)

    workers = min(max(1, int(workers)), len(pdfs))

    print(f"\n{'═'*58}")
    print(f"  COMPRIMIR PDFs (Ghostscript)")
    if dry_run:
        print("  *** MODO DRY-RUN: no se modificará ningún archivo ***")
    print(f"{'═'*58}")
    print(f"  Carpeta  : {carpeta.resolve()}")
    print(f"  Archivos : {len(pdfs)}")
    print(f"  Calidad  : {calidad}")
    print(f"  Workers  : {workers}\n")
    log.info("Compresión iniciada — %d PDFs, calidad %s", len(pdfs), calidad)

    if dry_run:
        print(f"  [DRY-RUN] Se comprimirían {len(pdfs)} PDFs in-place.")
        log.info("Dry-run: compresión simulada.")
        return

    carpeta_temp = Path(tempfile.mkdtemp(prefix="scw_comp_"))
    tareas = [(pdf, carpeta_temp, calidad, gs_cmd) for pdf in pdfs]
    reemplazados = sin_cambio = errores = 0
    t0 = time.time()

    try:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_comprimir_pdf, t): t[0] for t in tareas}
            hecho = 0
            for fut in concurrent.futures.as_completed(futures):
                hecho += 1
                orig, comp, ok, msg = fut.result()
                if not ok:
                    errores += 1
                    log.warning("Compresión falló %s: %s", orig.name, msg)
                elif comp == orig:
                    sin_cambio += 1
                else:
                    try:
                        os.replace(comp, orig)
                        reemplazados += 1
                    except OSError as e:
                        errores += 1
                        log.warning("No se pudo reemplazar %s: %s", orig.name, e)
                if hecho % 10 == 0 or hecho == len(pdfs):
                    elapsed = time.time() - t0
                    eta = (elapsed / hecho) * (len(pdfs) - hecho) if hecho else 0
                    pct = hecho * 100 // len(pdfs)
                    print(
                        f"  [{_barra(hecho, len(pdfs))}] {pct:3d}%"
                        f"  {hecho}/{len(pdfs)}  ETA {eta:.0f}s  err {errores}",
                        end="\r",
                    )
        print()
    finally:
        shutil.rmtree(carpeta_temp, ignore_errors=True)

    log.info(
        "Compresión terminada: reemplazados=%d sin_cambio=%d errores=%d",
        reemplazados,
        sin_cambio,
        errores,
    )
    print(f"\n{'─'*58}")
    print(f"  ✓  Reemplazados (más livianos) : {reemplazados}")
    print(f"  –  Sin cambio / igual tamaño   : {sin_cambio}")
    if errores:
        print(f"  !  Errores                     : {errores}")
    print(f"  📁  Carpeta                     : {carpeta.resolve()}")
    print(f"{'─'*58}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  COMANDO: UNIR
# ══════════════════════════════════════════════════════════════════════════════

def _buscar_ghostscript() -> str | None:
    # 1. Si estamos empaquetados con PyInstaller, buscar al lado del .exe
    base = Path(getattr(sys, "_MEIPASS", None) or sys.executable).parent
    for nombre in ["gswin64c.exe", "gswin32c.exe"]:
        candidato = base / nombre
        if candidato.exists():
            return str(candidato)
    # 2. Buscar en PATH del sistema
    for nombre in ["gs", "gswin64c", "gswin32c", "gsc"]:
        if shutil.which(nombre):
            return nombre
    return None

def _comprimir_pdf(args_tuple: tuple) -> tuple:
    ruta_original, carpeta_temp, calidad, gs_cmd = args_tuple
    ruta_comprimida = carpeta_temp / f"{ruta_original.stem}_c.pdf"
    cmd = [
        gs_cmd, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5",
        f"-dPDFSETTINGS=/{calidad}", "-dNOPAUSE", "-dQUIET", "-dBATCH",
        f"-sOutputFile={ruta_comprimida}", str(ruta_original),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        if ruta_comprimida.exists() and \
                ruta_comprimida.stat().st_size >= ruta_original.stat().st_size:
            ruta_comprimida.unlink()
            return (ruta_original, ruta_original, True, "sin cambio")
        return (ruta_original, ruta_comprimida, True, "ok")
    except subprocess.TimeoutExpired:
        return (ruta_original, ruta_original, False, "timeout")
    except Exception as e:
        return (ruta_original, ruta_original, False, f"error GS: {e}")

def _crear_pagina_separadora(nombre_archivo: str) -> bytes:
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    c.setFillColorRGB(*COLOR_AZUL)
    c.rect(0, h - 120, w, 120, fill=1, stroke=0)
    c.setFillColorRGB(0.7, 0.85, 1.0)
    c.setFont("Helvetica", 9)
    c.drawString(30, h - 22, "PODER JUDICIAL DE LA NACION  –  Sistema de Consulta Web")
    c.setStrokeColorRGB(0.5, 0.7, 0.9)
    c.setLineWidth(0.5)
    c.line(30, h - 30, w - 30, h - 30)

    c.setFillColorRGB(1, 1, 1)
    font_size = 15
    while font_size > 7:
        if c.stringWidth(nombre_archivo, "Helvetica-Bold", font_size) < w - 60:
            break
        font_size -= 1
    c.setFont("Helvetica-Bold", font_size)
    c.drawString(30, h - 75, nombre_archivo)

    partes  = nombre_archivo.split(" - ", 2)
    fecha   = partes[0].strip() if partes else ""
    tipo    = partes[1].strip() if len(partes) > 1 else ""
    detalle = partes[2].strip() if len(partes) > 2 else ""
    y = h - 160

    def fila(label, valor, y_pos):
        if not valor:
            return y_pos
        c.setFillColorRGB(*COLOR_AZUL)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(40, y_pos, label)
        c.setFillColorRGB(*COLOR_NEG)
        c.setFont("Helvetica", 9)
        c.drawString(130, y_pos, valor)
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.setLineWidth(0.3)
        c.line(40, y_pos - 4, w - 40, y_pos - 4)
        return y_pos - 22

    y = fila("Fecha:",       fecha,   y)
    y = fila("Tipo:",        tipo,    y)
    y = fila("Descripcion:", detalle, y)

    c.setFillColorRGB(*COLOR_AZUL)
    c.rect(0, 0, w, 28, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica", 7.5)
    c.drawCentredString(w / 2, 10, "scw.pjn.gov.ar  –  documento generado automaticamente")
    c.save()
    buf.seek(0)
    return buf.read()

def _agregar_pdf_seguro(ruta: Path, writer: PdfWriter) -> int:
    try:
        reader = PdfReader(str(ruta), strict=False)
        for page in reader.pages:
            writer.add_page(page)
        return len(reader.pages)
    except Exception as e:
        log.warning("SKIP '%s': %s", ruta.name, e)
        return 0


def _fecha_iso_desde_stem_pdf(stem: str) -> str | None:
    """
    Obtiene AAAA-MM-DD desde el nombre del archivo generado al descargar
    (p. ej. '2024-05-15 - Tipo - Detalle' o con sufijo ' (2)' por duplicados).
    """
    s = re.sub(r" \(\d+\)$", "", stem.strip())
    primera = s.split(" - ", 1)[0].strip() if " - " in s else s
    if not primera or primera.lower() == "sin_fecha":
        return None
    iso = _fecha_a_iso(primera)
    if iso:
        return iso
    return _fecha_a_iso(_parsear_fecha(primera))


def cmd_unir(args):
    """Une PDFs con página separadora por archivo; filtro opcional por fecha del documento (nombre de archivo)."""
    _verificar_deps([
        (_PYPDF_OK, "pypdf", "pypdf"),
        (_REPORTLAB_OK, "reportlab", "reportlab"),
    ])

    carpeta = (
        _path_cfg(args.carpeta)
        if getattr(args, "carpeta", None)
        else _path_cfg(CFG["carpeta_pdfs"])
    )
    salida = (
        _path_cfg(args.salida)
        if getattr(args, "salida", None)
        else _path_cfg(CFG["archivo_unificado"])
    )
    dry_run = getattr(args, "dry_run", False)

    ad = getattr(args, "desde", None)
    ah = getattr(args, "hasta", None)
    desde_raw = ad.strip() if isinstance(ad, str) and ad.strip() else CFG["unir_desde"].strip()
    hasta_raw = ah.strip() if isinstance(ah, str) and ah.strip() else CFG["unir_hasta"].strip()

    desde_iso = _fecha_a_iso(desde_raw) if desde_raw else None
    hasta_iso = _hasta_iso(hasta_raw) if hasta_raw else None
    if desde_raw and not desde_iso:
        log.error("No se pudo interpretar la fecha 'desde': %s", desde_raw)
        sys.exit(1)
    if hasta_raw and not hasta_iso:
        log.error("No se pudo interpretar la fecha 'hasta': %s", hasta_raw)
        sys.exit(1)

    if not carpeta.exists():
        log.error("La carpeta '%s' no existe.", carpeta)
        sys.exit(1)

    todos = sorted([p for p in carpeta.glob("*.pdf") if not p.name.startswith("_")])
    if not todos:
        log.error("No se encontraron PDFs en '%s'.", carpeta)
        sys.exit(1)

    filtro_activo = bool(desde_iso or hasta_iso)
    pdfs: list[Path] = []
    excluidos = 0
    sin_fecha = 0
    for p in todos:
        if not filtro_activo:
            pdfs.append(p)
            continue
        iso_doc = _fecha_iso_desde_stem_pdf(p.stem)
        if iso_doc is None:
            sin_fecha += 1
            excluidos += 1
            continue
        if _en_rango_fecha(iso_doc, desde_iso, hasta_iso):
            pdfs.append(p)
        else:
            excluidos += 1

    if filtro_activo and sin_fecha:
        log.info(
            "%d PDF(s) sin fecha reconocible en el nombre (omitidos con filtro activo).",
            sin_fecha,
        )
        print(f"  Info    : {sin_fecha} PDF(s) sin fecha en el nombre → omitidos (filtro activo).")

    if not pdfs:
        log.error(
            "Ningún PDF queda para unir%s.",
            " con el rango indicado" if filtro_activo else "",
        )
        sys.exit(1)

    print(f"\n{'═'*58}")
    print("  UNIR PDFs (separadores entre documentos)")
    if dry_run:
        print("  *** MODO DRY-RUN: no se escribirá ningún archivo ***")
    print(f"{'═'*58}")
    print(f"  Carpeta    : {carpeta.resolve()}")
    print(f"  PDFs total : {len(todos)}")
    if filtro_activo:
        print(f"  Tras filtro: {len(pdfs)}  (excluidos: {excluidos})")
        print(f"  Rango      : {desde_raw or '...'}  →  {hasta_raw or '...'}")
    else:
        print(f"  A unir     : {len(pdfs)}")
    print(f"  Salida     : {salida}")
    print("  Nota       : la compresión es el comando `comprimir` (paso aparte).\n")
    log.info("Unión iniciada — %d PDFs → %s", len(pdfs), salida)

    if dry_run:
        print(f"  [DRY-RUN] Se unirían {len(pdfs)} PDFs → '{salida}'")
        for i, p in enumerate(pdfs[:40], 1):
            print(f"    {i:3}. {p.name}")
        if len(pdfs) > 40:
            print(f"    ... y {len(pdfs) - 40} más.")
        log.info("Dry-run: unión simulada, %d PDFs.", len(pdfs))
        return

    writer = PdfWriter()
    pag_docs = skipped = 0
    n_sep = 0

    for i, pdf in enumerate(pdfs, 1):
        if i % 20 == 0 or i == len(pdfs):
            print(
                f"  [{_barra(i, len(pdfs))}] {i * 100 // len(pdfs):3d}%"
                f"  {i}/{len(pdfs)}",
                end="\r",
            )
        try:
            sep_reader = PdfReader(
                io.BytesIO(_crear_pagina_separadora(pdf.stem)), strict=False
            )
            for page in sep_reader.pages:
                writer.add_page(page)
            n_sep += len(sep_reader.pages)
        except Exception as e:
            log.warning("Separador '%s': %s", pdf.name, e)
        n = _agregar_pdf_seguro(pdf, writer)
        pag_docs += n
        if n == 0:
            skipped += 1

    print(f"\n\n  Guardando '{salida}'...")
    salida.parent.mkdir(parents=True, exist_ok=True)
    with open(salida, "wb") as f:
        writer.write(f)

    total_pag = n_sep + pag_docs
    log.info("Unificación completa: %d páginas, %s", total_pag, _mb(salida))
    print(f"\n{'─'*58}")
    print(f"  ✓  Archivos procesados : {len(pdfs) - skipped}/{len(pdfs)}")
    print(f"  📄  Páginas separador  : {n_sep}")
    print(f"  📄  Páginas documentos : {pag_docs}")
    print(f"  📄  Páginas totales    : {total_pag}")
    print(f"  💾  Tamaño final        : {_mb(salida)}")
    print(f"  📁  Archivo             : {salida.resolve()}")
    print(f"{'─'*58}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  COMANDO: TODO (flujo completo)
# ══════════════════════════════════════════════════════════════════════════════

def cmd_todo(args):
    dry_run = getattr(args, "dry_run", False)
    t_inicio = time.time()

    print(f"\n{'═'*58}")
    print("  FLUJO COMPLETO: Descargar → Comprimir → Unir")
    if dry_run:
        print("  *** MODO DRY-RUN ***")
    print(f"{'═'*58}")
    log.info("Flujo completo iniciado%s", " [DRY-RUN]" if dry_run else "")

    cmd_descargar(args)
    carp = _carpeta_trabajo_todo(args)
    setattr(args, "carpeta", carp)
    if getattr(args, "sin_comprimir", False):
        log.info("Flujo: compresión omitida (--sin-comprimir).")
    else:
        cmd_comprimir(args)
    cmd_unir(args)

    elapsed = time.time() - t_inicio
    mins, segs = divmod(int(elapsed), 60)

    print(f"\n{'═'*58}")
    print(f"  ✅  FLUJO COMPLETO  —  tiempo total: {mins}m {segs}s")
    print(f"{'═'*58}\n")
    log.info("Flujo completo terminado en %dm%ds", mins, segs)

    _notificar("SCW – Proceso completo",
               f"Expediente procesado en {mins}m {segs}s")
    _sonido_fin()


# ══════════════════════════════════════════════════════════════════════════════
#  MENÚ INTERACTIVO
# ══════════════════════════════════════════════════════════════════════════════

def _menu_interactivo():
    """Menú de texto cuando el script se corre sin argumentos."""
    opciones = {
        "1": ("Descargar PDFs del expediente",        "descargar"),
        "2": ("Comprimir PDFs (Ghostscript)",         "comprimir"),
        "3": ("Unir PDFs en un solo archivo",         "unir"),
        "4": ("Flujo completo (desc→comp→unir)",      "todo"),
        "5": ("Ver estado del proyecto",              "estado"),
        "0": ("Salir",                                "salir"),
    }

    while True:
        print(f"\n{'═'*58}")
        print("  SCW – Herramienta de expedientes judiciales")
        print(f"{'═'*58}")
        for k, (desc, _) in opciones.items():
            print(f"  [{k}]  {desc}")
        print(f"{'─'*58}")

        eleccion = input("  Opción: ").strip()

        if eleccion not in opciones:
            print("  Opción inválida.")
            continue

        _, cmd_nombre = opciones[eleccion]

        if cmd_nombre == "salir":
            print("  Hasta luego.\n")
            break

        # Construir args mínimos con defaults de config
        class _Args:
            url         = None
            carpeta     = None
            salida      = None
            calidad     = None
            workers     = None
            sin_comprimir = False
            concurrentes = None
            reintentos  = None
            dry_run     = False

        a = _Args()

        if cmd_nombre == "descargar":
            url_input = input(f"  URL (Enter = config.ini): ").strip()
            if url_input:
                a.url = url_input
            cmd_descargar(a)
        elif cmd_nombre == "comprimir":
            cmd_comprimir(a)
        elif cmd_nombre == "unir":
            cmd_unir(a)
        elif cmd_nombre == "todo":
            url_input = input(f"  URL (Enter = config.ini): ").strip()
            if url_input:
                a.url = url_input
            cmd_todo(a)
        elif cmd_nombre == "estado":
            cmd_estado(a)

        input("\n  [Enter para volver al menú]")


# ══════════════════════════════════════════════════════════════════════════════
#  ARGPARSE Y ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scw",
        description="SCW – Herramienta unificada para expedientes del Poder Judicial",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python -m pjn_scw.cli                     (menu interactivo)
  python -m pjn_scw.cli estado              (resumen del proyecto)
  python -m pjn_scw.cli descargar --url "https://..."
  python -m pjn_scw.cli comprimir --calidad ebook
  python -m pjn_scw.cli unir --desde 01/01/2023 --hasta 31/12/2024
  python -m pjn_scw.cli todo --dry-run
        """,
    )
    sub = parser.add_subparsers(dest="comando", metavar="COMANDO")

    # ── Argumentos comunes ────────────────────────────────────────────────────
    def _add_common(p):
        p.add_argument("--dry-run", action="store_true",
                       help="Simular sin ejecutar ni escribir archivos")

    # ── descargar ─────────────────────────────────────────────────────────────
    p_dl = sub.add_parser("descargar", help="Descargar PDFs del expediente SCW")
    p_dl.add_argument("--url",          type=str,  help="URL del expediente")
    p_dl.add_argument("--carpeta",      type=Path, help="Carpeta de destino")
    p_dl.add_argument("--concurrentes", type=int,  help="Descargas simultáneas")
    p_dl.add_argument("--reintentos",   type=int,  help="Reintentos por error")
    p_dl.add_argument("--desde",        type=str,  help="Fecha desde (DD/MM/AAAA)")
    p_dl.add_argument("--hasta",        type=str,  help="Fecha hasta (DD/MM/AAAA)")
    _add_common(p_dl)
    p_dl.set_defaults(func=cmd_descargar)

    # ── comprimir ─────────────────────────────────────────────────────────────
    p_co = sub.add_parser(
        "comprimir",
        help="Comprimir PDFs de una carpeta con Ghostscript (reemplaza si baja tamaño)",
    )
    p_co.add_argument("--carpeta",  type=Path, help="Carpeta con PDFs")
    p_co.add_argument(
        "--calidad",
        choices=["screen", "ebook", "printer", "prepress"],
        help="Calidad Ghostscript",
    )
    p_co.add_argument("--workers", type=int, help="Procesos en paralelo")
    _add_common(p_co)
    p_co.set_defaults(func=cmd_comprimir)

    # ── unir ──────────────────────────────────────────────────────────────────
    p_un = sub.add_parser(
        "unir",
        help="Unir PDFs con separadores; filtro por fechas opcional (nombre del archivo)",
    )
    p_un.add_argument("--carpeta", type=Path, help="Carpeta con PDFs")
    p_un.add_argument("--salida",  type=Path, help="Archivo PDF de salida")
    p_un.add_argument(
        "--desde",
        type=str,
        default=None,
        metavar="FECHA",
        help="Incluir solo documentos desde esta fecha (DD/MM/AAAA); default config.ini [union] desde",
    )
    p_un.add_argument(
        "--hasta",
        type=str,
        default=None,
        metavar="FECHA",
        help="Incluir solo documentos hasta esta fecha (DD/MM/AAAA); default config.ini [union] hasta",
    )
    _add_common(p_un)
    p_un.set_defaults(func=cmd_unir)

    # ── todo ──────────────────────────────────────────────────────────────────
    p_todo = sub.add_parser(
        "todo",
        help="Flujo completo: descargar + comprimir + unir",
    )
    p_todo.add_argument("--url",          type=str)
    p_todo.add_argument("--carpeta",      type=Path)
    p_todo.add_argument("--salida",       type=Path)
    p_todo.add_argument("--calidad",      choices=["screen","ebook","printer","prepress"])
    p_todo.add_argument("--workers",      type=int)
    p_todo.add_argument("--sin-comprimir",action="store_true")
    p_todo.add_argument("--concurrentes", type=int)
    p_todo.add_argument("--reintentos",   type=int)
    p_todo.add_argument("--desde",        type=str, help="Paso unir: fecha desde (DD/MM/AAAA)")
    p_todo.add_argument("--hasta",        type=str, help="Paso unir: fecha hasta (DD/MM/AAAA)")
    _add_common(p_todo)
    p_todo.set_defaults(func=cmd_todo)

    # ── estado ────────────────────────────────────────────────────────────────
    p_est = sub.add_parser("estado", help="Resumen de archivos y configuración")
    p_est.set_defaults(func=cmd_estado)

    return parser


def main():
    parser = _build_parser()
    args   = parser.parse_args()

    if args.comando is None:
        _menu_interactivo()
    else:
        args.func(args)


if __name__ == "__main__":
    main()
