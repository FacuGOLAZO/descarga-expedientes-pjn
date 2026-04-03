#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════════════════
  SCW – Herramienta unificada para expedientes del Poder Judicial
         Descargador · Unificador · Divisor
══════════════════════════════════════════════════════════════════
Requisitos:
    pip install -r requirements.txt
    playwright install chromium

Para compresión (opcional):
    Windows : https://www.ghostscript.com/download/gsdnld.html
    Mac     : brew install ghostscript
    Linux   : sudo apt install ghostscript

Uso (desde la raíz del repositorio):
    python -m pjn_scw.cli                ← menú interactivo
    python -m pjn_scw.cli descargar    ← solo descargar PDFs
    python -m pjn_scw.cli unir         ← solo unir PDFs
    python -m pjn_scw.cli dividir      ← solo dividir por año
    python -m pjn_scw.cli todo         ← los 3 pasos en secuencia
    python -m pjn_scw.cli todo --dry-run
    python -m pjn_scw.cli descargar --url "https://scw.pjn.gov.ar/..."
    python -m pjn_scw.cli unir --calidad screen --workers 8
    python -m pjn_scw.cli dividir --max-mb 25 --solo-año 2023
    python -m pjn_scw.cli estado       ← resumen de archivos existentes
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
    # union
    "archivo_unificado": Path(_cs("union", "archivo_salida",  "expediente_unificado.pdf")),
    "calidad":           _cs("union", "calidad",              "ebook"),
    "workers":           _ci("union", "workers",              max(1, os.cpu_count() // 2)),
    "sin_comprimir":     _cb("union", "sin_comprimir",        False),
    # division
    "carpeta_años":      Path(_cs("division", "carpeta_salida", "expediente_por_año")),
    "max_mb":            _cf("division", "max_mb",            40.0),
    "solo_año":          _cs("division", "solo_año",          "") or None,
}

# Constantes de diseño
COLOR_AZUL = (0.12, 0.31, 0.58)
COLOR_NEG  = (0.1, 0.1, 0.1)
PATRON_FECHA     = re.compile(r'\b(20\d{2})-\d{2}-\d{2}\b')
PATRON_FECHA_MES = re.compile(r'\b(20\d{2})-(\d{2})-\d{2}\b')


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
    carpeta_pdfs = CFG["carpeta_pdfs"]
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
    unificado = CFG["archivo_unificado"]
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

    # PDFs por año
    carpeta_años = CFG["carpeta_años"]
    print(f"\n  📂 PDFs por año  ({carpeta_años})")
    if carpeta_años.exists():
        partes = sorted(carpeta_años.glob("expediente_*.pdf"))
        if partes:
            for p in partes:
                alerta = "  ⚠" if p.stat().st_size / 1_048_576 > CFG["max_mb"] + 1 else ""
                print(f"     {p.name:<50} {_mb(p)}{alerta}")
        else:
            print("     (vacío)")
    else:
        print("     Carpeta no creada aún")

    # Config activa
    print(f"\n  ⚙  Configuración activa (config.ini)")
    print(f"     URL          : {CFG['url_default']}")
    print(f"     Concurrentes : {CFG['max_concurrentes']}")
    print(f"     Calidad GS   : {CFG['calidad']}")
    print(f"     Workers GS   : {CFG['workers']}")
    print(f"     Límite div.  : {CFG['max_mb']} MB")
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


def listar_expedientes(carpeta_base: Path | None = None) -> list[dict]:
    """
    Escanea la carpeta base y devuelve info de cada expediente registrado.
    Cada entrada: {nombre, carpeta, url, total, descargados, nuevos, ultima_act, tam_mb}
    """
    base = carpeta_base or CFG["carpeta_base"]
    if not base.exists():
        return []

    resultado = []
    for sub in sorted(base.iterdir()):
        if not sub.is_dir():
            continue
        reg_path = sub / NOMBRE_REGISTRO
        if not reg_path.exists():
            continue
        try:
            reg = json.loads(reg_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        docs      = reg.get("documentos", {})
        pdfs      = list(sub.glob("*.pdf"))
        tam_bytes = sum(p.stat().st_size for p in pdfs)

        resultado.append({
            "nombre":      reg.get("nombre", sub.name),
            "carpeta":     sub,
            "url":         reg.get("url", ""),
            "cid":         reg.get("cid", ""),
            "jurisdiccion": reg.get("jurisdiccion", ""),
            "numero":      reg.get("numero", ""),
            "anio":        reg.get("anio", ""),
            "total":       reg.get("total_documentos", len(docs)),
            "descargados": len([d for d in docs.values() if d.get("descargado_en")]),
            "errores":     len([d for d in docs.values() if d.get("error")]),
            "ultima_act":  reg.get("ultima_actualizacion", ""),
            "tam_mb":      tam_bytes / 1_048_576,
        })
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

def _periodo_en_rango(periodo: str, desde_iso: str | None, hasta_iso: str | None) -> bool:
    """
    Verifica si un período ('2023' o '2023-05') está dentro del rango.
    Un período se incluye si tiene cualquier superposición con el rango.
    """
    if not desde_iso and not hasta_iso:
        return True
    if periodo == "sin_fecha":
        return True  # siempre incluir documentos sin fecha

    # Convertir período a rango de fechas
    if re.match(r'^\d{4}-\d{2}$', periodo):
        p_desde = f"{periodo}-01"
        p_hasta = f"{periodo}-31"
    elif re.match(r'^\d{4}$', periodo):
        p_desde = f"{periodo}-01-01"
        p_hasta = f"{periodo}-12-31"
    else:
        return True  # formato desconocido, incluir

    # Superposición: el período empieza antes de que termine el rango
    #               Y el período termina después de que empieza el rango
    if hasta_iso and p_desde > hasta_iso:
        return False
    if desde_iso and p_hasta < desde_iso:
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
    filas = []
    rows  = await page.query_selector_all('#expediente\\:action-table tbody tr')
    for row in rows:
        link_el = await row.query_selector('a[href*="download=true"]')
        if not link_el:
            continue
        fecha_el = await row.query_selector('td:nth-child(3) span.font-color-black')
        tipo_el  = await row.query_selector('td:nth-child(4) span.font-color-black')
        det_el   = await row.query_selector('td:nth-child(5) span.font-color-black')
        if not (fecha_el and tipo_el):
            continue
        url_dl = await link_el.get_attribute('href')
        if not url_dl or 'viewer.seam' not in url_dl:
            continue
        if url_dl.startswith('/'):
            url_dl = 'https://scw.pjn.gov.ar' + url_dl
        filas.append({
            'fecha':   (await fecha_el.inner_text()).strip(),
            'tipo':    (await tipo_el.inner_text()).strip(),
            'detalle': ((await det_el.inner_text()).strip() if det_el else ''),
            'url':     url_dl,
        })
    return filas

async def _ir_siguiente_pagina(page, timeout_ajax: int) -> bool:
    siguiente = await page.query_selector('[id$=":divPagesAct"] span[title="Siguiente"]')
    if not siguiente:
        return False
    padre = await siguiente.evaluate_handle('el => el.parentElement')
    if (await padre.evaluate('el => el.tagName')).upper() != 'A':
        return False
    primera_antes = await page.evaluate('''() => {
        const td = document.querySelector(
            "#expediente\\\\:action-table tbody tr:first-child td:nth-child(3)"
        );
        return td ? td.innerText.trim() : "";
    }''')
    await padre.as_element().click()
    try:
        await page.wait_for_function(
            f'''() => {{
                const td = document.querySelector(
                    "#expediente\\\\:action-table tbody tr:first-child td:nth-child(3)"
                );
                return td && td.innerText.trim() !== {repr(primera_antes)};
            }}''',
            timeout=timeout_ajax * 1000,
        )
    except PWTimeout:
        pass
    await asyncio.sleep(CFG["pausa_pagina"])
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
                             cookies_jar, reintentos, dry_run, registro_docs):
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

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
        ),
        'Referer': 'https://scw.pjn.gov.ar/',
    }

    async with semaforo:
        for intento in range(1, reintentos + 1):
            try:
                async with httpx.AsyncClient(
                    cookies=cookies_jar, headers=headers,
                    follow_redirects=True, timeout=60,
                ) as client:
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
        while True:
            pag_actual = await _obtener_pagina_actual(page)
            label      = str(pag_actual) if pag_actual > 0 else str(pagina)
            filas      = await _extraer_filas(page)
            for fila in filas:
                fila['nombre'] = _construir_nombre(fila, conteo_nombres)
            todas_las_filas.extend(filas)
            print(f"  Pág. {label:>4} | {len(filas):>3} PDFs | "
                  f"acumulado: {len(todas_las_filas)}")
            hay_sig = await _ir_siguiente_pagina(page, CFG["timeout_ajax"])
            if not hay_sig:
                print(f"\n  Scraping completo: {len(todas_las_filas)} PDFs encontrados\n")
                break
            pagina += 1

        cookies_playwright = await context.cookies()
        await browser.close()

    if not todas_las_filas:
        log.warning("No se encontraron PDFs.")
        return

    log.info("Scraping completo: %d PDFs — %s", len(todas_las_filas), nombre_expediente)

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
        carpeta_base = CFG["carpeta_base"]
        carpeta_base.mkdir(exist_ok=True)
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

    tareas = [
        _descargar_un_pdf(
            semaforo, contador, lock, fila,
            carpeta / f"{fila['nombre']}.pdf",
            cookies_jar, reintentos, dry_run,
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

    log.info("Descarga completa — OK:%d  Omitidos:%d  Errores:%d  Tiempo:%dm%ds",
             contador['ok'], contador['omitidos'], contador['errores'], mins, segs)

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
    asyncio.run(_run_descargar(args))


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

def cmd_unir(args):
    _verificar_deps([
        (_PYPDF_OK,      "pypdf",     "pypdf"),
        (_REPORTLAB_OK,  "reportlab", "reportlab"),
    ])

    carpeta    = (args.carpeta or CFG["carpeta_pdfs"])
    salida     = (args.salida  or CFG["archivo_unificado"])
    calidad    = args.calidad  or CFG["calidad"]
    workers    = args.workers  or CFG["workers"]
    sin_comp   = args.sin_comprimir or CFG["sin_comprimir"]
    dry_run    = getattr(args, "dry_run", False)

    if not carpeta.exists():
        log.error("La carpeta '%s' no existe.", carpeta)
        sys.exit(1)

    pdfs = sorted([p for p in carpeta.glob("*.pdf") if not p.name.startswith("_")])
    if not pdfs:
        log.error("No se encontraron PDFs en '%s'.", carpeta)
        sys.exit(1)

    gs_cmd = _buscar_ghostscript()
    if not gs_cmd and not sin_comp:
        log.warning("Ghostscript no encontrado — se unirá sin comprimir.")
        sin_comp = True

    workers = min(workers, len(pdfs))

    print(f"\n{'═'*58}")
    print(f"  PASO 2 — Unificación de PDFs")
    if dry_run:
        print("  *** MODO DRY-RUN: no se escribirá ningún archivo ***")
    print(f"{'═'*58}")
    print(f"  Carpeta : {carpeta.resolve()}")
    print(f"  PDFs    : {len(pdfs)}")
    print(f"  Salida  : {salida}")
    print(f"  Calidad : {'sin comprimir' if sin_comp else calidad}")
    print(f"  Workers : {workers}\n")
    log.info("Unificación iniciada — %d PDFs, calidad: %s", len(pdfs), calidad)

    if dry_run:
        print(f"  [DRY-RUN] Se procesarían {len(pdfs)} PDFs → '{salida}'")
        log.info("Dry-run: unión simulada, %d PDFs.", len(pdfs))
        return

    # Fase 1: comprimir
    mapa: dict[Path, Path] = {}
    if sin_comp:
        print("  Fase 1/2 — Compresión omitida.\n")
        for pdf in pdfs:
            mapa[pdf] = pdf
    else:
        carpeta_temp = Path(tempfile.mkdtemp(prefix="scw_comp_"))
        print(f"  Fase 1/2 — Comprimiendo {len(pdfs)} PDFs...\n")
        tareas = [(pdf, carpeta_temp, calidad, gs_cmd) for pdf in pdfs]
        completados = errores = 0
        t0 = time.time()

        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_comprimir_pdf, t): t[0] for t in tareas}
            for fut in concurrent.futures.as_completed(futures):
                original, comprimido, exito, msg = fut.result()
                completados += 1
                if not exito:
                    errores += 1
                mapa[original] = comprimido
                if completados % 10 == 0 or completados == len(pdfs):
                    elapsed = time.time() - t0
                    eta     = (elapsed / completados) * (len(pdfs) - completados)
                    pct     = completados * 100 // len(pdfs)
                    print(
                        f"  [{_barra(completados, len(pdfs))}] {pct:3d}%"
                        f"  {completados}/{len(pdfs)}"
                        f"  ETA {eta:.0f}s  err {errores}",
                        end="\r"
                    )

        print(f"\n\n  Compresión completa — {completados} archivos, {errores} errores")
        log.info("Compresión completa: %d archivos, %d errores", completados, errores)

    # Fase 2: unir
    print(f"\n  Fase 2/2 — Uniendo {len(pdfs)} PDFs...\n")
    writer = PdfWriter()
    total_paginas = skipped = 0

    for i, pdf_orig in enumerate(pdfs, 1):
        pdf_usar = mapa.get(pdf_orig, pdf_orig)
        nombre   = pdf_orig.stem
        if i % 20 == 0 or i == len(pdfs):
            print(f"  [{_barra(i, len(pdfs))}] {i*100//len(pdfs):3d}%"
                  f"  {i}/{len(pdfs)}", end="\r")

        sep_bytes = _crear_pagina_separadora(nombre)
        sep_page  = PdfReader(io.BytesIO(sep_bytes)).pages[0]
        writer.add_page(sep_page)
        total_paginas += 1

        n = _agregar_pdf_seguro(pdf_usar, writer)
        total_paginas += n
        if n == 0:
            skipped += 1

    print(f"\n\n  Guardando '{salida}'...")
    with open(salida, "wb") as f:
        writer.write(f)

    if not sin_comp:
        shutil.rmtree(carpeta_temp, ignore_errors=True)

    log.info("Unificación completa: %d páginas, %s", total_paginas, _mb(salida))
    print(f"\n{'─'*58}")
    print(f"  ✓  Archivos procesados : {len(pdfs) - skipped}/{len(pdfs)}")
    print(f"  📄  Páginas totales     : {total_paginas}")
    print(f"  💾  Tamaño final        : {_mb(salida)}")
    print(f"  📁  Archivo             : {salida.resolve()}")
    print(f"{'─'*58}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  COMANDO: DIVIDIR
# ══════════════════════════════════════════════════════════════════════════════

_MESES_ES = {
    "01": "Enero", "02": "Febrero", "03": "Marzo",    "04": "Abril",
    "05": "Mayo",  "06": "Junio",   "07": "Julio",    "08": "Agosto",
    "09": "Septiembre", "10": "Octubre", "11": "Noviembre", "12": "Diciembre",
}

def _detectar_periodo(page, modo: str = "año") -> str | None:
    """
    Detecta el período de una página.
    modo='año'  → devuelve '2023'
    modo='mes'  → devuelve '2023-05'
    """
    try:
        texto = page.extract_text() or ""
        m = PATRON_FECHA_MES.search(texto)
        if m:
            año, mes = m.group(1), m.group(2)
            return año if modo == "año" else f"{año}-{mes}"
    except Exception:
        pass
    return None

def _detectar_año(page) -> str | None:
    return _detectar_periodo(page, "año")

def _bytes_reales(pages: list) -> int:
    w = PdfWriter()
    for p in pages:
        w.add_page(p)
    buf = io.BytesIO()
    w.write(buf)
    return buf.tell()

def _medir_paginas(pages: list) -> list[int]:
    sizes = []
    for i, page in enumerate(pages):
        if i % 500 == 0 and i > 0:
            print(f"    midiendo: {i}/{len(pages)}...", end="\r")
        w = PdfWriter()
        w.add_page(page)
        buf = io.BytesIO()
        w.write(buf)
        sizes.append(buf.tell())
    return sizes

def _guardar(pages: list, ruta: Path) -> int:
    w = PdfWriter()
    for p in pages:
        w.add_page(p)
    with open(ruta, "wb") as f:
        w.write(f)
    return ruta.stat().st_size

def _dividir_en_partes(pages, max_bytes, nombre_base, carpeta):
    sizes      = _medir_paginas(pages)
    archivos   = []
    lote_idx   = []
    acum_bytes = 0
    num_parte  = 1

    for i, (page, sz) in enumerate(zip(pages, sizes)):
        if acum_bytes + sz >= max_bytes and lote_idx:
            lote_pages = [pages[j] for j in lote_idx]
            nombre     = carpeta / f"{nombre_base}_parte{num_parte}.pdf"
            tam_real   = _guardar(lote_pages, nombre)
            archivos.append(nombre)
            print(f"    {nombre.name:<52} {tam_real/1_048_576:>6.1f} MB  "
                  f"({len(lote_pages)} págs.)")

            if tam_real > max_bytes and len(lote_pages) > 1:
                archivos.pop()
                nombre.unlink(missing_ok=True)
                mitad = len(lote_pages) // 2
                for chunk in [lote_pages[:mitad], lote_pages[mitad:]]:
                    n2  = carpeta / f"{nombre_base}_parte{num_parte}.pdf"
                    t2  = _guardar(chunk, n2)
                    archivos.append(n2)
                    print(f"    {n2.name:<52} {t2/1_048_576:>6.1f} MB  "
                          f"({len(chunk)} págs.)  [ajustado]")
                    num_parte += 1
            else:
                num_parte += 1

            lote_idx   = []
            acum_bytes = 0

        lote_idx.append(i)
        acum_bytes += sz

    if lote_idx:
        lote_pages = [pages[j] for j in lote_idx]
        nombre = (
            carpeta / f"{nombre_base}.pdf" if num_parte == 1
            else carpeta / f"{nombre_base}_parte{num_parte}.pdf"
        )
        tam_real = _guardar(lote_pages, nombre)
        archivos.append(nombre)
        print(f"    {nombre.name:<52} {tam_real/1_048_576:>6.1f} MB  "
              f"({len(lote_pages)} págs.)")

    return archivos

def cmd_dividir(args):
    _verificar_deps([(_PYPDF_OK, "pypdf", "pypdf")])

    entrada   = getattr(args, "entrada", None) or CFG["archivo_unificado"]
    carpeta   = getattr(args, "salida",  None) or CFG["carpeta_años"]
    max_bytes = int((getattr(args, "max_mb",   None) or CFG["max_mb"]) * 1_048_576)
    solo_año  = getattr(args, "solo_año",  None) or CFG["solo_año"]
    solo_mes  = getattr(args, "solo_mes",  None) or None
    modo      = getattr(args, "modo",      "año")
    desde_raw = getattr(args, "desde",     None) or ""
    hasta_raw = getattr(args, "hasta",     None) or ""
    dry_run   = getattr(args, "dry_run",   False)
    max_mb    = max_bytes / 1_048_576

    if solo_mes:
        modo = "mes"

    # Normalizar rango
    desde_iso = _fecha_a_iso(desde_raw) if desde_raw else None
    hasta_iso = _hasta_iso(hasta_raw)   if hasta_raw else None

    if not entrada.exists():
        log.error("No se encontró '%s'.", entrada)
        sys.exit(1)

    carpeta.mkdir(exist_ok=True)

    modo_label = "mes" if modo == "mes" else "año"
    print(f"\n{'═'*58}")
    print(f"  PASO 3 — División por {modo_label}")
    if dry_run:
        print("  *** MODO DRY-RUN: no se escribirá ningún archivo ***")
    print(f"{'═'*58}")
    print(f"  Entrada  : {entrada}  ({_mb(entrada)})")
    print(f"  Salida   : {carpeta.resolve()}")
    print(f"  Modo     : por {modo_label}")
    print(f"  Límite   : {max_mb} MB por archivo")

    # Mostrar filtro activo
    if solo_mes:
        año_f, mes_f = solo_mes.split("-") if "-" in solo_mes else (solo_mes, "")
        nombre_mes = _MESES_ES.get(mes_f, mes_f)
        print(f"  Filtro   : {nombre_mes} {año_f}")
    elif solo_año:
        print(f"  Filtro   : {solo_año}")
    elif desde_raw or hasta_raw:
        print(f"  Rango    : {desde_raw or '...'} → {hasta_raw or '...'}")
    else:
        print(f"  Filtro   : {'todos los meses' if modo == 'mes' else 'todos los años'}")
    print()
    log.info("División iniciada — entrada: %s  modo: %s", entrada, modo_label)

    print("  Leyendo PDF...")
    reader  = PdfReader(str(entrada), strict=False)
    n_total = len(reader.pages)
    print(f"  Total de páginas: {n_total}\n")
    print("  Escaneando páginas separadoras...")

    paginas_por_periodo: dict[str, list] = {}
    periodo_actual = "sin_fecha"

    for i, page in enumerate(reader.pages):
        if i % 500 == 0:
            print(f"    {i}/{n_total}...", end="\r")
        periodo = _detectar_periodo(page, modo)
        if periodo:
            periodo_actual = periodo
        paginas_por_periodo.setdefault(periodo_actual, []).append(page)

    print(f"    {n_total}/{n_total} páginas escaneadas.     \n")

    periodos = sorted(k for k in paginas_por_periodo if k != "sin_fecha")
    if "sin_fecha" in paginas_por_periodo:
        periodos.append("sin_fecha")

    # Mostrar resumen
    if modo == "mes":
        print(f"  Períodos detectados ({len(periodos)} meses):")
        for p in periodos:
            if "-" in p:
                año_p, mes_p = p.split("-")
                etiq = f"{_MESES_ES.get(mes_p, mes_p)} {año_p}"
            else:
                etiq = p
            print(f"    {p}  ({etiq}) : {len(paginas_por_periodo[p])} páginas")
    else:
        print(f"  Años detectados:")
        for p in periodos:
            print(f"    {p} : {len(paginas_por_periodo[p])} páginas")

    # Aplicar filtros
    if solo_mes:
        if solo_mes not in paginas_por_periodo:
            log.error("Período '%s' no encontrado.", solo_mes)
            sys.exit(1)
        paginas_por_periodo = {solo_mes: paginas_por_periodo[solo_mes]}
        periodos = [solo_mes]
    elif solo_año:
        filtrado = {k: v for k, v in paginas_por_periodo.items()
                    if k.startswith(solo_año)}
        if not filtrado:
            log.error("Año '%s' no encontrado.", solo_año)
            sys.exit(1)
        paginas_por_periodo = filtrado
        periodos = sorted(filtrado)

    # Filtrar por rango de fechas (desde/hasta)
    if desde_iso or hasta_iso:
        antes = len(paginas_por_periodo)
        paginas_por_periodo = {
            k: v for k, v in paginas_por_periodo.items()
            if _periodo_en_rango(k, desde_iso, hasta_iso)
        }
        periodos = sorted(k for k in paginas_por_periodo if k != "sin_fecha")
        if "sin_fecha" in paginas_por_periodo:
            periodos.append("sin_fecha")
        excluidos = antes - len(paginas_por_periodo)
        if excluidos:
            print(f"\n  Filtro de fechas: {excluidos} período(s) excluido(s).")
        if not paginas_por_periodo:
            log.warning("Ningún período cae dentro del rango de fechas indicado.")
            return

    if dry_run:
        print("\n  [DRY-RUN] Archivos que se generarían:")
        for p in sorted(paginas_por_periodo):
            n = len(paginas_por_periodo[p])
            print(f"    expediente_{p}.pdf  (~{n} páginas)")
        log.info("Dry-run: división simulada.")
        return

    print(f"\n  Generando archivos...\n")
    total_archivos = 0

    for periodo in sorted(paginas_por_periodo):
        pages = paginas_por_periodo[periodo]

        # Etiqueta legible para la consola
        if modo == "mes" and "-" in periodo:
            año_p, mes_p = periodo.split("-")
            etiq = f"{_MESES_ES.get(mes_p, mes_p)} {año_p}"
        else:
            etiq = periodo

        nombre_base = f"expediente_{periodo}"
        print(f"  [{etiq}]  {len(pages)} páginas")
        tam = _bytes_reales(pages)

        if tam <= max_bytes:
            ruta     = carpeta / f"{nombre_base}.pdf"
            tam_real = _guardar(pages, ruta)
            print(f"    {ruta.name:<52} {tam_real/1_048_576:>6.1f} MB")
            total_archivos += 1
        else:
            print(f"    ~{tam/1_048_576:.0f} MB → dividiendo en partes de {max_mb} MB...")
            archivos = _dividir_en_partes(pages, max_bytes, nombre_base, carpeta)
            total_archivos += len(archivos)

    generados = sorted(carpeta.glob("expediente_*.pdf"))
    tam_total  = sum(f.stat().st_size for f in generados) / 1_048_576

    log.info("División completa: %d archivos, %.1f MB", total_archivos, tam_total)
    print(f"\n{'─'*58}")
    print(f"  ✓  Archivos generados : {total_archivos}")
    print(f"  💾  Tamaño total       : {tam_total:.1f} MB")
    print(f"  📁  Carpeta            : {carpeta.resolve()}")
    print(f"{'─'*58}\n")
    for f in generados:
        mb = f.stat().st_size / 1_048_576
        alerta = "  ⚠ SUPERA LÍMITE" if mb > max_mb + 1 else ""
        print(f"  {f.name:<52} {mb:>6.1f} MB{alerta}")
    print()


# ══════════════════════════════════════════════════════════════════════════════
#  COMANDO: TODO (flujo completo)
# ══════════════════════════════════════════════════════════════════════════════

def cmd_todo(args):
    dry_run = getattr(args, "dry_run", False)
    t_inicio = time.time()

    print(f"\n{'═'*58}")
    print("  FLUJO COMPLETO: Descargar → Unir → Dividir")
    if dry_run:
        print("  *** MODO DRY-RUN ***")
    print(f"{'═'*58}")
    log.info("Flujo completo iniciado%s", " [DRY-RUN]" if dry_run else "")

    cmd_descargar(args)
    cmd_unir(args)
    cmd_dividir(args)

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
        "1": ("Descargar PDFs del expediente",   "descargar"),
        "2": ("Unir PDFs en un solo archivo",     "unir"),
        "3": ("Dividir por año",                  "dividir"),
        "4": ("Flujo completo (1+2+3)",           "todo"),
        "5": ("Ver estado del proyecto",          "estado"),
        "0": ("Salir",                            "salir"),
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
            entrada     = None
            max_mb      = None
            solo_año    = None
            solo_mes    = None
            modo        = "año"
            concurrentes = None
            reintentos  = None
            dry_run     = False

        a = _Args()

        if cmd_nombre == "descargar":
            url_input = input(f"  URL (Enter = config.ini): ").strip()
            if url_input:
                a.url = url_input
            cmd_descargar(a)
        elif cmd_nombre == "unir":
            cmd_unir(a)
        elif cmd_nombre == "dividir":
            modo_input = input(f"  Modo — [a]ño / [m]es (Enter = año): ").strip().lower()
            a.modo = "mes" if modo_input.startswith("m") else "año"
            if a.modo == "mes":
                mes_input = input(f"  Solo mes (ej: 2023-05, Enter = todos): ").strip()
                if mes_input:
                    a.solo_mes = mes_input
            else:
                año_input = input(f"  Solo año (Enter = todos): ").strip()
                if año_input:
                    a.solo_año = año_input
            cmd_dividir(a)
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
  python -m pjn_scw.cli unir --calidad screen
  python -m pjn_scw.cli dividir --max-mb 25 --solo-año 2023
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

    # ── unir ──────────────────────────────────────────────────────────────────
    p_un = sub.add_parser("unir", help="Unir PDFs en un solo archivo")
    p_un.add_argument("--carpeta",       type=Path, help="Carpeta con PDFs")
    p_un.add_argument("--salida",        type=Path, help="Archivo PDF de salida")
    p_un.add_argument("--calidad",       choices=["screen","ebook","printer","prepress"],
                      help="Calidad Ghostscript")
    p_un.add_argument("--workers",       type=int,  help="Procesos GS en paralelo")
    p_un.add_argument("--sin-comprimir", action="store_true",
                      help="Omitir Ghostscript")
    _add_common(p_un)
    p_un.set_defaults(func=cmd_unir)

    # ── dividir ───────────────────────────────────────────────────────────────
    p_div = sub.add_parser("dividir", help="Dividir el PDF unificado por año o mes")
    p_div.add_argument("--entrada",   type=Path,  help="PDF a dividir")
    p_div.add_argument("--salida",    type=Path,  help="Carpeta de salida")
    p_div.add_argument("--max-mb",    type=float, help="Tamaño máximo por archivo")
    p_div.add_argument("--modo",      choices=["año", "mes"], default="año",
                       help="Dividir por año (default) o por mes")
    p_div.add_argument("--solo-año",  type=str,   help="Exportar solo un año (ej: 2023)")
    p_div.add_argument("--solo-mes",  type=str,   help="Exportar solo un mes (ej: 2023-05)")
    p_div.add_argument("--desde",     type=str,   help="Fecha desde (DD/MM/AAAA)")
    p_div.add_argument("--hasta",     type=str,   help="Fecha hasta (DD/MM/AAAA)")
    _add_common(p_div)
    p_div.set_defaults(func=cmd_dividir)

    # ── todo ──────────────────────────────────────────────────────────────────
    p_todo = sub.add_parser("todo", help="Flujo completo: descargar + unir + dividir")
    p_todo.add_argument("--url",          type=str)
    p_todo.add_argument("--carpeta",      type=Path)
    p_todo.add_argument("--salida",       type=Path)
    p_todo.add_argument("--calidad",      choices=["screen","ebook","printer","prepress"])
    p_todo.add_argument("--workers",      type=int)
    p_todo.add_argument("--sin-comprimir",action="store_true")
    p_todo.add_argument("--concurrentes", type=int)
    p_todo.add_argument("--reintentos",   type=int)
    p_todo.add_argument("--max-mb",       type=float)
    p_todo.add_argument("--solo-año",     type=str)
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
