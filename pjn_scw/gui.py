#!/usr/bin/env python3
"""
Interfaz gráfica para SCW (Expedientes Judiciales PJN).
Ejecutar desde la raíz del proyecto:  python -m pjn_scw.gui
Requiere las mismas dependencias que la CLI (ver requirements.txt).
"""

import sys
import os
import io
import threading
import queue
import logging
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import configparser

# ══════════════════════════════════════════════════════════════════
#  CAPTURA DE STDOUT/LOGGING → COLA
# ══════════════════════════════════════════════════════════════════

_log_queue: queue.Queue = queue.Queue()


class _QueueStream(io.TextIOBase):
    """Redirige print() / stdout a la cola del log."""

    def __init__(self, q: queue.Queue):
        self._q = q

    def write(self, text: str) -> int:
        if text:
            self._q.put(text)
        return len(text or "")

    def flush(self):
        pass


class _QueueLogHandler(logging.Handler):
    """Redirige logging a la cola del log."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self._q = q
        self.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                              datefmt="%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord):
        self._q.put(self.format(record) + "\n")


# ── Importar lógica CLI y enchufar el handler de logging ──────────
try:
    import pjn_scw.cli as scw
    _q_handler = _QueueLogHandler(_log_queue)
    scw.log.addHandler(_q_handler)
    _SCW_OK = True
except ImportError:
    _SCW_OK = False


# ══════════════════════════════════════════════════════════════════
#  CONSTANTES DE DISEÑO
# ══════════════════════════════════════════════════════════════════

# Paleta
C_SIDEBAR_BG   = "#12253d"
C_SIDEBAR_SEL  = "#1e3f64"
C_SIDEBAR_HVR  = "#1a3452"
C_SIDEBAR_FG   = "#c8daf0"
C_SIDEBAR_MUTED= "#6e8fac"

C_CONTENT_BG   = "#f4f7fb"
C_WHITE        = "#ffffff"
C_BORDER       = "#dde3ec"

C_ACCENT       = "#1a5fac"
C_ACCENT_DARK  = "#134a88"
C_ACCENT_LIGHT = "#e8f0fc"

C_LOG_BG       = "#0e1117"
C_LOG_FG       = "#c9d1d9"
C_LOG_OK       = "#56d364"
C_LOG_ERR      = "#f85149"
C_LOG_WARN     = "#e3b341"
C_LOG_INFO     = "#79c0ff"

C_TEXT         = "#1a2332"
C_TEXT_MUTED   = "#5a6a7d"

# Tipografía
FONT_UI        = ("Segoe UI", 9)
FONT_UI_BOLD   = ("Segoe UI", 9, "bold")
FONT_TITLE     = ("Segoe UI", 13, "bold")
FONT_SECTION   = ("Segoe UI", 10, "bold")
FONT_SIDEBAR   = ("Segoe UI", 10)
FONT_MONO      = ("Consolas", 9)

PAD = 10


# ══════════════════════════════════════════════════════════════════
#  HELPERS CONFIG
# ══════════════════════════════════════════════════════════════════

def _cfg_path() -> Path:
    if _SCW_OK:
        return scw.project_base() / "config.ini"
    return Path(__file__).resolve().parent.parent / "config.ini"


def _leer_cfg() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    ruta = _cfg_path()
    if ruta.exists():
        cfg.read(ruta, encoding="utf-8")
    return cfg


def _guardar_cfg(cfg: configparser.ConfigParser):
    with open(_cfg_path(), "w", encoding="utf-8") as f:
        cfg.write(f)


# ══════════════════════════════════════════════════════════════════
#  WIDGET HELPERS
# ══════════════════════════════════════════════════════════════════

def _section_label(parent, text: str) -> tk.Label:
    lbl = tk.Label(parent, text=text, font=FONT_SECTION,
                   bg=C_WHITE, fg=C_TEXT)
    return lbl


def _make_field(parent, row: int, label: str, default="",
                width=38, is_check=False, is_combo=False,
                combo_values=None):
    """Crea label + control en un grid de 3 columnas. Devuelve la Variable."""
    tk.Label(parent, text=label, font=FONT_UI,
             bg=C_WHITE, fg=C_TEXT_MUTED,
             anchor="w").grid(row=row, column=0, sticky="w",
                              padx=(0, 14), pady=4)

    if is_check:
        var = tk.BooleanVar(value=bool(default))
        cb = tk.Checkbutton(parent, variable=var,
                            bg=C_WHITE, activebackground=C_WHITE,
                            cursor="hand2")
        cb.grid(row=row, column=1, sticky="w", pady=4)
        return var

    if is_combo:
        var = tk.StringVar(value=str(default))
        cb = ttk.Combobox(parent, textvariable=var,
                          values=combo_values or [],
                          state="readonly", width=width - 4,
                          font=FONT_UI)
        cb.grid(row=row, column=1, sticky="ew", pady=4)
        return var

    var = tk.StringVar(value=str(default))
    ent = tk.Entry(parent, textvariable=var, width=width,
                   font=FONT_UI, relief="solid",
                   bd=1, bg=C_WHITE, fg=C_TEXT,
                   highlightthickness=1,
                   highlightbackground=C_BORDER,
                   highlightcolor=C_ACCENT)
    ent.grid(row=row, column=1, sticky="ew", pady=4)
    return var


def _btn(parent, text, command, style="normal", **kw):
    """Botón con estilo propio (no depende de ttk styles complejos)."""
    if style == "accent":
        bg, fg, abg = C_ACCENT, "#ffffff", C_ACCENT_DARK
    elif style == "ghost":
        bg, fg, abg = C_CONTENT_BG, C_TEXT, C_BORDER
    else:
        bg, fg, abg = C_WHITE, C_TEXT, C_CONTENT_BG

    btn = tk.Button(parent, text=text, command=command,
                    bg=bg, fg=fg, activebackground=abg,
                    activeforeground=fg,
                    font=FONT_UI_BOLD if style == "accent" else FONT_UI,
                    relief="flat", bd=0, padx=14, pady=6,
                    cursor="hand2", **kw)
    return btn


class Tooltip:
    """
    Tooltip flotante genérico.

    content puede ser:
      - str  → texto plano simple
      - list[str]  → párrafos separados
      - list[tuple(str, str, str)]  → filas tipo (título, subtítulo, descripción)
        usadas para la tabla de calidades GS
    """

    def __init__(self, widget: tk.Widget, title: str, content):
        self._widget  = widget
        self._title   = title
        self._content = content
        self._win     = None
        widget.bind("<Enter>",       self._show)
        widget.bind("<Leave>",       self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _show(self, event=None):
        if self._win:
            return
        x = self._widget.winfo_rootx() + self._widget.winfo_width() + 8
        y = self._widget.winfo_rooty()

        self._win = tk.Toplevel(self._widget)
        self._win.wm_overrideredirect(True)
        self._win.attributes("-topmost", True)

        outer = tk.Frame(self._win, bg=C_BORDER, padx=1, pady=1)
        outer.pack()
        inner = tk.Frame(outer, bg="#1e2d3d", padx=14, pady=10)
        inner.pack()

        # Título
        tk.Label(inner, text=self._title,
                 font=("Segoe UI", 9, "bold"),
                 bg="#1e2d3d", fg="#ffffff").pack(anchor="w", pady=(0, 6))

        # Separador fino bajo el título
        tk.Frame(inner, bg="#2d3f52", height=1).pack(fill="x", pady=(0, 8))

        # Contenido: lista de tuplas (tabla) o lista de strings (párrafos)
        if self._content and isinstance(self._content[0], tuple):
            # Modo tabla: (nombre, badge, descripción)
            for i, (name, badge, desc) in enumerate(self._content):
                row_f = tk.Frame(inner, bg="#1e2d3d")
                row_f.pack(fill="x", pady=(0, 5))

                hdr_f = tk.Frame(row_f, bg="#1e2d3d")
                hdr_f.pack(anchor="w")
                tk.Label(hdr_f, text=name,
                         font=("Consolas", 9, "bold"),
                         bg="#1e2d3d", fg=C_LOG_INFO,
                         width=12, anchor="w").pack(side="left")
                if badge:
                    tk.Label(hdr_f, text=badge,
                             font=("Segoe UI", 8),
                             bg="#1e2d3d", fg=C_SIDEBAR_MUTED,
                             anchor="w").pack(side="left")

                tk.Label(row_f, text=desc,
                         font=("Segoe UI", 8),
                         bg="#1e2d3d", fg="#8b949e",
                         justify="left", anchor="w",
                         wraplength=270).pack(anchor="w", padx=(2, 0))

                if i < len(self._content) - 1:
                    tk.Frame(row_f, bg="#2d3f52", height=1).pack(
                        fill="x", pady=(5, 0))
        else:
            # Modo párrafos: lista de strings
            for line in self._content:
                tk.Label(inner, text=line,
                         font=("Segoe UI", 8),
                         bg="#1e2d3d", fg="#8b949e",
                         justify="left", anchor="w",
                         wraplength=280).pack(anchor="w", pady=(0, 3))

        # Posicionar después de construir para conocer el tamaño real
        self._win.update_idletasks()
        w = self._win.winfo_width()
        h = self._win.winfo_height()
        sw = self._win.winfo_screenwidth()
        sh = self._win.winfo_screenheight()
        # Ajustar si se sale de la pantalla
        if x + w > sw:
            x = self._widget.winfo_rootx() - w - 6
        if y + h > sh:
            y = sh - h - 10
        self._win.wm_geometry(f"+{x}+{y}")

    def _hide(self, event=None):
        if self._win:
            self._win.destroy()
            self._win = None


# ── Definiciones de todos los tooltips ────────────────────────────

_TT = {
    "url": (
        "URL del expediente",
        [
            "Es la dirección web del expediente en el sitio del Poder Judicial.",
            "La encontrás en la barra de direcciones de tu navegador cuando\nestás viendo el expediente en scw.pjn.gov.ar.",
            "El número después de 'cid=' identifica el expediente.\nCambialo si querés descargar uno diferente.",
        ]
    ),
    "carpeta_salida": (
        "Carpeta de salida",
        [
            "Es la carpeta donde se van a guardar los PDFs descargados.",
            "Podés escribir una ruta (ej: C:\\Expedientes\\caso123)\no usar el botón 📁 para elegirla desde el explorador.",
            "Si la carpeta no existe, se crea automáticamente.",
        ]
    ),
    "concurrentes": (
        "Descargas simultáneas",
        [
            "Cuántos PDFs se descargan al mismo tiempo en paralelo.",
            "Más alto = termina más rápido, pero usa más ancho de banda\ny puede sobrecargar el servidor.",
            "Valor recomendado: entre 4 y 12.\nSi el servidor devuelve errores, bajalo a 4.",
        ]
    ),
    "reintentos": (
        "Reintentos por error",
        [
            "Si un PDF falla al descargar, el programa lo vuelve a intentar\nesta cantidad de veces antes de marcarlo como error.",
            "Con 3 reintentos alcanza para la mayoría de los casos.\nSi la conexión es inestable, podés subir a 5.",
        ]
    ),
    "pausa_pagina": (
        "Pausa entre páginas (segundos)",
        [
            "El sitio SCW carga los documentos de a páginas mediante\ntecnología AJAX (sin recargar la página).",
            "Esta pausa le da tiempo al servidor para terminar de\ncargar cada página antes de continuar.",
            "Con 4 seg es conservador y confiable.\nSi tenés buena conexión podés bajar a 2.\nSi faltan documentos al terminar, subilo a 6.",
        ]
    ),
    "dry_run": (
        "Modo simulación (Dry-run)",
        [
            "Cuando está activado, el programa recorre todo el proceso\npero NO descarga ni crea ningún archivo.",
            "Útil para ver cuántos documentos hay, verificar la URL\no estimar cuánto va a tardar, sin riesgo de nada.",
            "El log va a mostrar qué haría, con la etiqueta DRY-RUN.",
        ]
    ),
    "gs_calidad": (
        "Calidad de compresión (Ghostscript)",
        [
            ("screen",   "~72 dpi",  "Mínimo peso. Ideal para enviar por mail o WhatsApp.\nEl texto puede verse algo borroso al imprimir."),
            ("ebook",    "~150 dpi", "Equilibrio entre tamaño y calidad. Recomendado\npara la mayoría de los casos."),
            ("printer",  "~300 dpi", "Alta calidad, apto para imprimir. El archivo\nqueda bastante más grande que ebook."),
            ("prepress", "~300 dpi", "Máxima calidad, para imprenta profesional.\nEn la práctica es igual a printer para uso judicial."),
        ]
    ),
    "workers": (
        "Workers (procesos en paralelo)",
        [
            "Cuántos archivos se comprimen al mismo tiempo.",
            "Más workers = compresión más rápida, pero usa más\nmemoria RAM y procesador.",
            "El valor por defecto es la mitad de los núcleos de tu CPU.\nSi la PC se pone lenta durante el proceso, bajalo a 2.",
        ]
    ),
    "sin_comprimir": (
        "Sin comprimir",
        [
            "Omite la compresión con Ghostscript y une los PDFs\ntal como están.",
            "Usá esta opción si:\n• Ghostscript no está instalado\n• Los PDFs ya están comprimidos\n• Querés una unión rápida sin perder calidad",
            "El archivo final va a ser más grande.",
        ]
    ),
    "max_mb": (
        "Tamaño máximo por archivo (MB)",
        [
            "Al dividir el expediente por año, si un año tiene\nmuchos documentos, el PDF resultante puede ser enorme.",
            "Con este límite, si un año supera el tamaño indicado,\nse divide automáticamente en partes: 2022_parte1, 2022_parte2, etc.",
            "40 MB es un buen valor para poder adjuntar en\ncorreos electrónicos o sistemas judiciales.",
        ]
    ),
    "solo_año": (
        "Filtrar por año",
        [
            "Si lo dejás vacío, se exportan todos los años\ndetectados en el expediente.",
            "Si escribís un año (ej: 2023), solo se genera\nel PDF de ese año.",
            "Útil si solo necesitás un período específico\nsin procesar todo el expediente.",
        ]
    ),
    "desde_hasta": (
        "Rango de fechas",
        [
            "Filtra los documentos por fecha.",
            "Formato: DD/MM/AAAA  (ej: 15/03/2021)",
            "Podés dejar uno vacío:\n• Solo 'Desde' → todos desde esa fecha en adelante\n• Solo 'Hasta' → todos hasta esa fecha\n• Ambos vacíos → sin filtro, se procesan todos",
            "Para descargar: filtra qué documentos descarga.\nPara dividir: filtra qué períodos genera.",
        ]
    ),
    "solo_mes": (
        "Filtrar por mes",
        [
            "Si lo dejás vacío, se exportan todos los meses detectados.",
            "Si escribís un mes en formato AAAA-MM (ej: 2023-05),\nsolo se genera el PDF de ese mes.",
            "Útil para buscar documentos de un período específico.",
        ]
    ),
    "modo_division": (
        "Modo de división",
        [
            ("año", "",  "Un PDF por cada año del expediente.\nEj: expediente_2021.pdf, expediente_2022.pdf\nIdeal para expedientes con pocos documentos por año."),
            ("mes", "",  "Un PDF por cada mes del expediente.\nEj: expediente_2023-01.pdf, expediente_2023-02.pdf\nIdeal cuando un año tiene demasiados documentos."),
        ]
    ),
}


def _question_btn(parent, row: int, col: int, tip_key: str) -> tk.Label:
    """Pequeño botón ? con tooltip asociado al tip_key."""
    title, content = _TT[tip_key]
    lbl = tk.Label(parent, text=" ? ",
                   font=("Segoe UI", 7, "bold"),
                   bg=C_ACCENT_LIGHT, fg=C_ACCENT,
                   relief="flat", cursor="hand2",
                   padx=3, pady=1)
    lbl.grid(row=row, column=col, padx=(4, 0), pady=4, sticky="w")
    Tooltip(lbl, title, content)
    return lbl


def _card(parent, **kw) -> tk.Frame:
    """Frame tipo tarjeta con borde sutil."""
    outer = tk.Frame(parent, bg=C_BORDER, padx=1, pady=1)
    inner = tk.Frame(outer, bg=C_WHITE, **kw)
    inner.pack(fill="both", expand=True)
    return outer, inner


# ══════════════════════════════════════════════════════════════════
#  PÁGINAS
# ══════════════════════════════════════════════════════════════════

class BasePage(tk.Frame):
    """
    Página base con:
    - Header fijo en la parte superior
    - Cuerpo scrollable con canvas + scrollbar
    - _build_card / _build_date_range / _build_actions paquetan en self._body
    """

    def __init__(self, parent, app):
        super().__init__(parent, bg=C_CONTENT_BG)
        self.app   = app
        self._body = None   # se asigna al llamar _build_header

    # ── Header ────────────────────────────────────────────────────

    def _build_header(self, icon: str, title: str, subtitle: str = ""):
        hdr = tk.Frame(self, bg=C_WHITE, pady=12)
        hdr.pack(fill="x", side="top")
        tk.Frame(hdr, bg=C_BORDER, height=1).pack(side="bottom", fill="x")

        inner = tk.Frame(hdr, bg=C_WHITE)
        inner.pack(anchor="w", padx=20)

        tk.Label(inner, text=f"{icon}  {title}", font=FONT_TITLE,
                 bg=C_WHITE, fg=C_TEXT).pack(anchor="w")
        if subtitle:
            tk.Label(inner, text=subtitle, font=("Segoe UI", 8),
                     bg=C_WHITE, fg=C_TEXT_MUTED).pack(anchor="w", pady=(1, 0))

        # Crear el área scrollable que ocupa el resto de la página
        self._setup_scroll()

    def _setup_scroll(self):
        """Crea canvas + scrollbar + frame interno (self._body)."""
        wrapper = tk.Frame(self, bg=C_CONTENT_BG)
        wrapper.pack(fill="both", expand=True)

        canvas = tk.Canvas(wrapper, bg=C_CONTENT_BG,
                           highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(wrapper, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)

        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._body = tk.Frame(canvas, bg=C_CONTENT_BG)
        win_id = canvas.create_window((0, 0), window=self._body, anchor="nw")

        def _on_canvas_resize(e):
            canvas.itemconfig(win_id, width=e.width)
        canvas.bind("<Configure>", _on_canvas_resize)

        def _on_frame_resize(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        self._body.bind("<Configure>", _on_frame_resize)

        def _on_scroll(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind("<Enter>",  lambda _: canvas.bind_all("<MouseWheel>", _on_scroll))
        canvas.bind("<Leave>",  lambda _: canvas.unbind_all("<MouseWheel>"))

        self._canvas = canvas

    # ── Cards y controles (paquetan en self._body) ─────────────────

    def _build_card(self, title: str = "") -> tk.Frame:
        outer = tk.Frame(self._body, bg=C_CONTENT_BG)
        outer.pack(fill="x", padx=14, pady=(8, 0))

        if title:
            tk.Label(outer, text=title, font=FONT_SECTION,
                     bg=C_CONTENT_BG, fg=C_TEXT).pack(anchor="w", pady=(0, 4))

        border = tk.Frame(outer, bg=C_BORDER, padx=1, pady=1)
        border.pack(fill="x")
        card = tk.Frame(border, bg=C_WHITE, padx=14, pady=10)
        card.pack(fill="both", expand=True)
        card.columnconfigure(1, weight=1)
        return card

    def _build_date_range(self, card: tk.Frame,
                          row_desde: int = 0, row_hasta: int = 1):
        """Campos Desde / Hasta. Guarda en self.v_desde / self.v_hasta."""
        self.v_desde = _make_field(card, row_desde, "Desde (DD/MM/AAAA)", "", width=14)
        _question_btn(card, row_desde, 2, "desde_hasta")

        self.v_hasta = _make_field(card, row_hasta, "Hasta (DD/MM/AAAA)", "", width=14)
        _question_btn(card, row_hasta, 2, "desde_hasta")

        tk.Label(card,
                 text="Dejá ambos vacíos para procesar todos los documentos.",
                 font=("Segoe UI", 7), bg=C_WHITE, fg=C_TEXT_MUTED).grid(
                     row=row_hasta + 1, column=0, columnspan=3,
                     sticky="w", pady=(0, 2))

    def _build_actions(self, buttons: list):
        """Barra de botones al pie del cuerpo scrollable."""
        bar = tk.Frame(self._body, bg=C_CONTENT_BG)
        bar.pack(fill="x", padx=14, pady=10)
        for text, cmd, style in buttons:
            _btn(bar, text, cmd, style=style).pack(side="left", padx=(0, 8))

    def _file_btn(self, parent, row: int, var: tk.StringVar,
                  kind: str = "open", filetypes=None):
        """Botón 📁/💾 al lado de un campo en el grid."""
        icon = "📂" if kind == "open" else "💾" if kind == "save" else "📁"
        def _pick():
            if kind == "open":
                p = filedialog.askopenfilename(
                    filetypes=filetypes or [("PDF", "*.pdf"), ("Todos", "*.*")])
            elif kind == "save":
                p = filedialog.asksaveasfilename(
                    defaultextension=".pdf",
                    filetypes=filetypes or [("PDF", "*.pdf")])
            else:
                p = filedialog.askdirectory()
            if p:
                var.set(p)
        btn = tk.Button(parent, text=icon, command=_pick,
                        bg=C_WHITE, relief="flat", bd=0,
                        font=("Segoe UI", 11), cursor="hand2",
                        activebackground=C_ACCENT_LIGHT)
        btn.grid(row=row, column=2, padx=(6, 0), pady=4)


# ─── Descargar ────────────────────────────────────────────────────

class PageDescargar(BasePage):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        cfg = _leer_cfg()

        self._build_header("⬇", "Descargar PDFs",
                           "Abre el expediente en Chromium y descarga todos los documentos")

        card = self._build_card("Configuración de descarga")

        url_def = cfg.get("descarga", "url_default",
                          fallback="https://scw.pjn.gov.ar/scw/expediente.seam?cid=682804")
        self.v_url = _make_field(card, 0, "URL del expediente", url_def, width=52)
        _question_btn(card, 0, 2, "url")

        carp_def = cfg.get("descarga", "carpeta_salida", fallback="expediente_pdfs")
        self.v_carpeta = _make_field(card, 1, "Carpeta de salida", carp_def)
        self._file_btn(card, 1, self.v_carpeta, kind="dir")
        _question_btn(card, 1, 3, "carpeta_salida")

        conc_def = cfg.get("descarga", "max_concurrentes", fallback="8")
        self.v_conc = _make_field(card, 2, "Descargas simultáneas", conc_def, width=8)
        _question_btn(card, 2, 2, "concurrentes")

        ret_def = cfg.get("descarga", "reintentos", fallback="3")
        self.v_ret = _make_field(card, 3, "Reintentos por error", ret_def, width=8)
        _question_btn(card, 3, 2, "reintentos")

        pausa_def = cfg.get("descarga", "pausa_pagina", fallback="4.0")
        self.v_pausa = _make_field(card, 4, "Pausa entre páginas (seg)", pausa_def, width=8)
        _question_btn(card, 4, 2, "pausa_pagina")

        self.v_dry = _make_field(card, 5, "Dry-run (simular sin descargar)", False,
                                 is_check=True)
        _question_btn(card, 5, 2, "dry_run")

        # Datos de búsqueda
        card_login = self._build_card("Datos de búsqueda (se completan automáticamente)")

        # Dropdown de jurisdicción con todas las opciones del sitio SCW
        JURISDICCIONES = [
            "",
            "CSJ - Corte Suprema de Justicia de la Nación",
            "CIV - Cámara Nacional de Apelaciones en lo Civil",
            "CAF - Cámara Nacional de Apelaciones en lo Contencioso Administrativo Federal",
            "CCF - Cámara Nacional de Apelaciones en lo Civil y Comercial Federal",
            "CNE - Cámara Nacional Electoral",
            "CSS - Camara Federal de la Seguridad Social",
            "CPE - Cámara Nacional de Apelaciones en lo Penal Económico",
            "CNT - Cámara Nacional de Apelaciones del Trabajo",
            "CFP - Camara Criminal y Correccional Federal",
            "CCC - Camara Nacional de Apelaciones en lo Criminal y Correccional",
            "COM - Camara Nacional de Apelaciones en lo Comercial",
            "CPF - Camara Federal de Casación Penal",
            "CPN - Camara Nacional Casacion Penal",
            "FBB - Justicia Federal de Bahia Blanca",
            "FCR - Justicia Federal de Comodoro Rivadavia",
            "FCB - Justicia Federal de Córdoba",
            "FCT - Justicia Federal de Corrientes",
            "FGR - Justicia Federal de General Roca",
            "FLP - Justicia Federal de La Plata",
            "FMP - Justicia Federal de Mar del Plata",
            "FMZ - Justicia Federal de Mendoza",
            "FPO - Justicia Federal de Posadas",
            "FPA - Justicia Federal de Paraná",
            "FRE - Justicia Federal de Resistencia",
            "FSA - Justicia Federal de Salta",
            "FRO - Justicia Federal de Rosario",
            "FSM - Justicia Federal de San Martin",
            "FTU - Justicia Federal de Tucuman",
        ]

        tk.Label(card_login, text="Jurisdicción", font=("Segoe UI", 9),
                 bg="#ffffff", fg="#6b7a99",
                 anchor="w").grid(row=0, column=0, sticky="w", padx=(10, 6), pady=4)
        self.v_jurisdiccion = tk.StringVar(value="")
        self._combo_jurisdiccion = ttk.Combobox(
            card_login,
            textvariable=self.v_jurisdiccion,
            values=JURISDICCIONES,
            state="readonly",
            width=48,
            font=("Segoe UI", 9),
        )
        self._combo_jurisdiccion.grid(row=0, column=1, sticky="w", padx=(0, 6), pady=4)

        self.v_numero = _make_field(card_login, 1, "Número", "", width=12)
        self.v_anio   = _make_field(card_login, 2, "Año",    "", width=8)

        # Rango de fechas
        card2 = self._build_card("Filtro de fechas (opcional)")
        self._build_date_range(card2, row_desde=0, row_hasta=1)

        self._build_actions([
            ("💾  Guardar en config.ini", self._guardar, "ghost"),
            ("▶  Ejecutar descarga",      self._run,     "accent"),
        ])

    def precargar_expediente(self, exp: dict):
        """Rellena los campos con los datos de un expediente ya registrado."""
        if exp.get("url"):
            self.v_url.set(exp["url"])
        if exp.get("jurisdiccion"):
            jur = exp["jurisdiccion"]
            # Si el valor guardado es solo el código (ej: "COM"), buscar el label completo
            opciones = self._combo_jurisdiccion["values"]
            match = next((o for o in opciones if o.startswith(jur)), jur)
            self.v_jurisdiccion.set(match)
        if exp.get("numero"):
            self.v_numero.set(exp["numero"])
        if exp.get("anio"):
            self.v_anio.set(exp["anio"])

    def _guardar(self):
        cfg = _leer_cfg()
        cfg.setdefault("descarga", {})
        cfg["descarga"]["url_default"]      = self.v_url.get()
        cfg["descarga"]["carpeta_salida"]   = self.v_carpeta.get()
        cfg["descarga"]["max_concurrentes"] = self.v_conc.get()
        cfg["descarga"]["reintentos"]       = self.v_ret.get()
        cfg["descarga"]["pausa_pagina"]     = self.v_pausa.get()
        _guardar_cfg(cfg)
        self.app.set_status("✅  config.ini guardado.")

    def _run(self):
        desde = self.v_desde.get().strip() if hasattr(self, 'v_desde') else ""
        hasta = self.v_hasta.get().strip() if hasattr(self, 'v_hasta') else ""

        class Args:
            url          = self.v_url.get() or None
            carpeta      = Path(self.v_carpeta.get()) if self.v_carpeta.get() else None
            concurrentes = int(self.v_conc.get() or 8)
            reintentos   = int(self.v_ret.get() or 3)
            dry_run      = self.v_dry.get()
            jurisdiccion = self.v_jurisdiccion.get().strip()
            numero       = self.v_numero.get().strip()
            anio         = self.v_anio.get().strip()

        Args.desde = desde or None
        Args.hasta = hasta or None
        self.app.run_task(scw.cmd_descargar, Args())


# ─── Unir ─────────────────────────────────────────────────────────

class PageUnir(BasePage):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        cfg = _leer_cfg()

        self._build_header("📎", "Unir PDFs",
                           "Comprime y une todos los PDFs en un único archivo con páginas separadoras")

        # ── Banner de estado Ghostscript ──────────────────────────
        self._build_gs_banner()

        card = self._build_card("Configuración de unión")

        carp_def = cfg.get("descarga", "carpeta_salida", fallback="expediente_pdfs")
        self.v_carpeta = _make_field(card, 0, "Carpeta de PDFs", carp_def)
        self._file_btn(card, 0, self.v_carpeta, kind="dir")

        sal_def = cfg.get("union", "archivo_salida", fallback="expediente_unificado.pdf")
        self.v_salida = _make_field(card, 1, "Archivo de salida", sal_def)
        self._file_btn(card, 1, self.v_salida, kind="save")

        cal_def = cfg.get("union", "calidad", fallback="ebook")
        self.v_calidad = _make_field(card, 2, "Calidad Ghostscript", cal_def,
                                     is_combo=True,
                                     combo_values=["screen", "ebook", "printer", "prepress"],
                                     width=14)
        _question_btn(card, 2, 2, "gs_calidad")

        wrk_def = cfg.get("union", "workers",
                          fallback=str(max(1, (os.cpu_count() or 4) // 2)))
        self.v_workers = _make_field(card, 3, "Workers Ghostscript", wrk_def, width=8)
        _question_btn(card, 3, 2, "workers")

        sin_def = cfg.getboolean("union", "sin_comprimir", fallback=False)
        self.v_sincomp = _make_field(card, 4, "Sin comprimir (omitir Ghostscript)",
                                     sin_def, is_check=True)
        _question_btn(card, 4, 2, "sin_comprimir")

        self.v_dry     = _make_field(card, 5, "Dry-run (simular)", False, is_check=True)
        _question_btn(card, 5, 2, "dry_run")

        self._build_actions([
            ("💾  Guardar en config.ini", self._guardar, "ghost"),
            ("▶  Ejecutar unión",         self._run,     "accent"),
        ])

    def _build_gs_banner(self):
        """Banner que muestra si Ghostscript está instalado o no."""
        import shutil as _sh

        gs_cmd = None
        for nombre in ["gswin64c", "gswin32c", "gs", "gsc"]:
            if _sh.which(nombre):
                gs_cmd = nombre
                break

        version = ""
        if gs_cmd:
            try:
                import subprocess as _sp
                r = _sp.run([gs_cmd, "--version"],
                            capture_output=True, text=True, timeout=5)
                version = r.stdout.strip()
            except Exception:
                version = "instalado"

        outer = tk.Frame(self._body, bg=C_CONTENT_BG)
        outer.pack(fill="x", padx=14, pady=(8, 0))

        if gs_cmd:
            bg_c  = "#e6f4ea"
            fg_c  = "#1a7f37"
            dot_c = "#2da44e"
            icon  = "✔"
            l1    = f"Ghostscript  {version}"
            l2    = f"Compresión disponible  ·  ejecutable: {gs_cmd}"
        else:
            bg_c  = "#fff8e6"
            fg_c  = "#7d4e00"
            dot_c = "#e3b341"
            icon  = "⚠"
            l1    = "Ghostscript no encontrado"
            l2    = ("La unión se hará sin comprimir (archivos más grandes).\n"
                     "Instalalo para habilitar la compresión.")

        banner = tk.Frame(outer, bg=bg_c, padx=14, pady=10,
                          highlightbackground=dot_c, highlightthickness=1)
        banner.pack(fill="x")

        # Ícono
        tk.Label(banner, text=icon, font=("Segoe UI", 18),
                 bg=bg_c, fg=dot_c).pack(side="left", padx=(0, 12))

        # Texto
        txt = tk.Frame(banner, bg=bg_c)
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text=l1, font=("Segoe UI", 9, "bold"),
                 bg=bg_c, fg=fg_c, anchor="w").pack(anchor="w")
        tk.Label(txt, text=l2, font=("Segoe UI", 8),
                 bg=bg_c, fg=fg_c, anchor="w", justify="left").pack(
                     anchor="w", pady=(1, 0))

        # Botón de descarga solo si no está instalado
        if not gs_cmd:
            def _abrir():
                import webbrowser
                webbrowser.open(
                    "https://www.ghostscript.com/download/gsdnld.html")
            tk.Button(banner, text="Descargar Ghostscript →",
                      bg=bg_c, fg="#0969da",
                      relief="flat", bd=0,
                      font=("Segoe UI", 8, "underline"),
                      cursor="hand2",
                      activebackground=bg_c,
                      activeforeground="#0550ae",
                      command=_abrir).pack(side="right", padx=(8, 0))

    def _guardar(self):
        cfg = _leer_cfg()
        cfg.setdefault("union", {})
        cfg["union"]["archivo_salida"] = self.v_salida.get()
        cfg["union"]["calidad"]        = self.v_calidad.get()
        cfg["union"]["workers"]        = self.v_workers.get()
        cfg["union"]["sin_comprimir"]  = str(self.v_sincomp.get()).lower()
        _guardar_cfg(cfg)
        self.app.set_status("✅  config.ini guardado.")

    def _run(self):
        class Args:
            carpeta       = Path(self.v_carpeta.get()) if self.v_carpeta.get() else None
            salida        = Path(self.v_salida.get()) if self.v_salida.get() else None
            calidad       = self.v_calidad.get() or None
            workers       = int(self.v_workers.get() or 4)
            sin_comprimir = self.v_sincomp.get()
            dry_run       = self.v_dry.get()
        self.app.run_task(scw.cmd_unir, Args())


# ─── Dividir ──────────────────────────────────────────────────────

class PageDividir(BasePage):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        cfg = _leer_cfg()

        self._build_header("✂", "Dividir por período",
                           "Divide el PDF unificado por año o por mes con límite de tamaño configurable")

        card = self._build_card("Configuración de división")

        ent_def = cfg.get("division", "archivo_entrada",
                          fallback="expediente_unificado.pdf")
        self.v_entrada = _make_field(card, 0, "PDF a dividir", ent_def)
        self._file_btn(card, 0, self.v_entrada, kind="open")

        sal_def = cfg.get("division", "carpeta_salida", fallback="expediente_por_año")
        self.v_salida = _make_field(card, 1, "Carpeta de salida", sal_def)
        self._file_btn(card, 1, self.v_salida, kind="dir")
        _question_btn(card, 1, 3, "carpeta_salida")

        mb_def = cfg.get("division", "max_mb", fallback="40")
        self.v_maxmb = _make_field(card, 2, "Tamaño máximo por archivo (MB)",
                                   mb_def, width=8)
        _question_btn(card, 2, 2, "max_mb")

        # Modo: año / mes
        self.v_modo = _make_field(card, 3, "Dividir por",
                                  "año", is_combo=True,
                                  combo_values=["año", "mes"], width=8)
        _question_btn(card, 3, 2, "modo_division")

        # Fila de filtro — cambia según el modo
        self._filtro_lbl = tk.Label(card,
                                    text="Solo un año (vacío = todos)",
                                    font=FONT_UI, bg=C_WHITE,
                                    fg=C_TEXT_MUTED, anchor="w")
        self._filtro_lbl.grid(row=4, column=0, sticky="w", padx=(0, 14), pady=4)

        self.v_filtro = tk.StringVar(value="")
        self._filtro_ent = tk.Entry(card, textvariable=self.v_filtro,
                                    width=12, font=FONT_UI, relief="solid",
                                    bd=1, bg=C_WHITE, fg=C_TEXT,
                                    highlightthickness=1,
                                    highlightbackground=C_BORDER,
                                    highlightcolor=C_ACCENT)
        self._filtro_ent.grid(row=4, column=1, sticky="w", pady=4)
        self._q_filtro = _question_btn(card, 4, 2, "solo_año")

        # Nota de ejemplo dinámica
        self._nota_filtro = tk.Label(card, text="Ej: 2023",
                                     font=("Segoe UI", 7),
                                     bg=C_WHITE, fg=C_TEXT_MUTED, anchor="w")
        self._nota_filtro.grid(row=4, column=3, sticky="w", padx=(8, 0), pady=4)

        self.v_dry = _make_field(card, 5, "Dry-run (simular)", False, is_check=True)
        _question_btn(card, 5, 2, "dry_run")

        # Actualizar labels cuando cambia el modo
        self.v_modo.trace_add("write", self._on_modo_change)

        # Card de rango de fechas
        card2 = self._build_card("Rango de fechas (opcional)")
        self._build_date_range(card2, row_desde=0, row_hasta=1)

        self._build_actions([
            ("💾  Guardar en config.ini", self._guardar, "ghost"),
            ("▶  Ejecutar división",      self._run,     "accent"),
        ])

    def _on_modo_change(self, *_):
        if self.v_modo.get() == "mes":
            self._filtro_lbl.config(text="Solo un mes (vacío = todos)")
            self._nota_filtro.config(text="Ej: 2023-05")
            # Actualizar tooltip del filtro
            self._q_filtro.destroy()
            self._q_filtro = _question_btn(
                self._filtro_lbl.master, 4, 2, "solo_mes")
        else:
            self._filtro_lbl.config(text="Solo un año (vacío = todos)")
            self._nota_filtro.config(text="Ej: 2023")
            self._q_filtro.destroy()
            self._q_filtro = _question_btn(
                self._filtro_lbl.master, 4, 2, "solo_año")

    def _guardar(self):
        cfg = _leer_cfg()
        cfg.setdefault("division", {})
        cfg["division"]["archivo_entrada"] = self.v_entrada.get()
        cfg["division"]["carpeta_salida"]  = self.v_salida.get()
        cfg["division"]["max_mb"]          = self.v_maxmb.get()
        _guardar_cfg(cfg)
        self.app.set_status("✅  config.ini guardado.")

    def _run(self):
        modo   = self.v_modo.get()
        filtro = self.v_filtro.get().strip() or None
        desde  = self.v_desde.get().strip() if hasattr(self, 'v_desde') else ""
        hasta  = self.v_hasta.get().strip() if hasattr(self, 'v_hasta') else ""

        class Args:
            entrada  = Path(self.v_entrada.get()) if self.v_entrada.get() else None
            salida   = Path(self.v_salida.get()) if self.v_salida.get() else None
            max_mb   = float(self.v_maxmb.get() or 40)
            solo_año = filtro if modo == "año" else None
            solo_mes = filtro if modo == "mes" else None
            dry_run  = self.v_dry.get()

        Args.modo  = modo
        Args.desde = desde or None
        Args.hasta = hasta or None
        self.app.run_task(scw.cmd_dividir, Args())


# ─── Flujo completo ───────────────────────────────────────────────

class PageTodo(BasePage):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        cfg = _leer_cfg()

        self._build_header("🔄", "Flujo completo",
                           "Ejecuta los 3 pasos en secuencia: Descargar → Unir → Dividir")

        # Pasos visuales
        steps_frame = tk.Frame(self._body, bg=C_CONTENT_BG)
        steps_frame.pack(fill="x", padx=14, pady=(8, 0))

        steps = [
            ("1", "⬇", "Descargar",  "Scraping + descarga de PDFs"),
            ("2", "📎", "Unir",       "Comprimir y unificar"),
            ("3", "✂", "Dividir",    "Separar por año"),
        ]
        for num, icon, title, desc in steps:
            s = tk.Frame(steps_frame, bg=C_BORDER, padx=1, pady=1)
            s.pack(side="left", fill="both", expand=True,
                   padx=(0, 8) if num != "3" else 0)
            inner = tk.Frame(s, bg=C_WHITE, padx=14, pady=12)
            inner.pack(fill="both", expand=True)
            tk.Label(inner, text=f"{num}", font=("Segoe UI", 20, "bold"),
                     bg=C_WHITE, fg=C_ACCENT).pack(anchor="w")
            tk.Label(inner, text=f"{icon} {title}", font=FONT_SECTION,
                     bg=C_WHITE, fg=C_TEXT).pack(anchor="w", pady=(2, 0))
            tk.Label(inner, text=desc, font=("Segoe UI", 8),
                     bg=C_WHITE, fg=C_TEXT_MUTED).pack(anchor="w")

        card = self._build_card("Parámetros")

        url_def = cfg.get("descarga", "url_default",
                          fallback="https://scw.pjn.gov.ar/scw/expediente.seam?cid=682804")
        self.v_url = _make_field(card, 0, "URL del expediente", url_def, width=52)
        _question_btn(card, 0, 2, "url")
        self.v_dry = _make_field(card, 1, "Dry-run (simular todo sin ejecutar)", False,
                                 is_check=True)
        _question_btn(card, 1, 2, "dry_run")

        self._build_actions([
            ("▶▶  Ejecutar flujo completo", self._run, "accent"),
        ])

    def _run(self):
        class Args:
            url           = self.v_url.get() or None
            carpeta       = None
            salida        = None
            calidad       = None
            workers       = None
            sin_comprimir = False
            entrada       = None
            max_mb        = None
            solo_año      = None
            concurrentes  = None
            reintentos    = None
            dry_run       = self.v_dry.get()
        self.app.run_task(scw.cmd_todo, Args())


# ─── Expedientes registrados ──────────────────────────────────────

class PageExpedientes(BasePage):
    """Lista todos los expedientes descargados con opción de actualizar."""

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self._cards: list[tk.Frame] = []
        self._build_header("📂", "Mis expedientes",
                           "Todos los expedientes descargados — podés actualizar cada uno")
        self._build_toolbar()
        self.after(100, self.refrescar)

    # ── Toolbar (va dentro de _body, encima de las cards) ────────

    def _build_toolbar(self):
        bar = tk.Frame(self._body, bg=C_CONTENT_BG)
        bar.pack(fill="x", padx=14, pady=(8, 0))
        _btn(bar, "🔄  Refrescar lista", self.refrescar, style="ghost").pack(side="left")
        self._lbl_total = tk.Label(bar, text="", font=("Segoe UI", 8),
                                   bg=C_CONTENT_BG, fg=C_TEXT_MUTED)
        self._lbl_total.pack(side="right")

    # ── Refrescar lista ───────────────────────────────────────────

    def refrescar(self):
        # Limpiar todo excepto el toolbar (primer hijo)
        children = self._body.winfo_children()
        for w in children[1:]:   # saltar el toolbar
            w.destroy()
        self._cards.clear()

        if not _SCW_OK:
            tk.Label(self._body, text="Módulo pjn_scw.cli no disponible",
                     bg=C_CONTENT_BG, fg=C_TEXT_MUTED,
                     font=FONT_UI).pack(pady=40)
            return

        expedientes = scw.listar_expedientes()

        if not expedientes:
            self._mostrar_vacio()
        else:
            for exp in expedientes:
                self._build_exp_card(exp)

        total      = len(expedientes)
        total_docs = sum(e["total"] for e in expedientes)
        tam_total  = sum(e["tam_mb"] for e in expedientes)
        self._lbl_total.config(
            text=f"{total} expediente{'s' if total != 1 else ''}  ·  "
                 f"{total_docs} documentos  ·  {tam_total:.0f} MB"
        )

    def _mostrar_vacio(self):
        frame = tk.Frame(self._body, bg=C_CONTENT_BG)
        frame.pack(fill="x", pady=60)
        tk.Label(frame, text="📭", font=("Segoe UI", 36),
                 bg=C_CONTENT_BG, fg=C_BORDER).pack()
        tk.Label(frame,
                 text="Todavía no descargaste ningún expediente.",
                 font=("Segoe UI", 10), bg=C_CONTENT_BG,
                 fg=C_TEXT_MUTED).pack(pady=(8, 0))
        tk.Label(frame,
                 text="Usá la sección ⬇ Descargar para empezar.",
                 font=("Segoe UI", 9), bg=C_CONTENT_BG,
                 fg=C_TEXT_MUTED).pack(pady=(4, 0))

    # ── Card de expediente ────────────────────────────────────────

    def _build_exp_card(self, exp: dict):
        outer = tk.Frame(self._body, bg=C_CONTENT_BG)
        outer.pack(fill="x", padx=14, pady=(0, 8))

        # Borde con color según estado
        tiene_nuevos = exp["total"] > exp["descargados"]
        borde_color  = C_ACCENT if tiene_nuevos else C_BORDER
        border = tk.Frame(outer, bg=borde_color, padx=1, pady=1)
        border.pack(fill="x")
        card = tk.Frame(border, bg=C_WHITE, padx=16, pady=12)
        card.pack(fill="x")

        # Fila superior: nombre + botones
        top = tk.Frame(card, bg=C_WHITE)
        top.pack(fill="x")

        # Nombre del expediente
        nombre_frame = tk.Frame(top, bg=C_WHITE)
        nombre_frame.pack(side="left", fill="x", expand=True)

        tk.Label(nombre_frame, text=exp["nombre"],
                 font=("Segoe UI", 10, "bold"),
                 bg=C_WHITE, fg=C_TEXT,
                 anchor="w", justify="left",
                 wraplength=500).pack(anchor="w")

        # Badge "N nuevos" si hay
        pendientes = exp["total"] - exp["descargados"]
        if pendientes > 0:
            badge = tk.Label(nombre_frame,
                             text=f"  {pendientes} nuevo{'s' if pendientes != 1 else ''}  ",
                             font=("Segoe UI", 8, "bold"),
                             bg=C_ACCENT, fg="#ffffff",
                             padx=4, pady=1)
            badge.pack(anchor="w", pady=(3, 0))

        # Botones al costado derecho
        btn_frame = tk.Frame(top, bg=C_WHITE)
        btn_frame.pack(side="right", padx=(12, 0))

        _btn(btn_frame, "📁  Abrir carpeta",
             lambda c=exp["carpeta"]: self._abrir_carpeta(c),
             style="ghost").pack(side="left", padx=(0, 6))

        _btn(btn_frame, "✏  Renombrar",
             lambda e=exp: self._renombrar(e),
             style="ghost").pack(side="left", padx=(0, 6))

        _btn(btn_frame, "🔄  Actualizar",
             lambda e=exp: self._actualizar(e),
             style="accent").pack(side="left")

        _btn(btn_frame, "⬇  Descargar",
             lambda e=exp: self._ir_a_descargar(e),
             style="ghost").pack(side="left", padx=(6, 0))

        # Separador
        tk.Frame(card, bg=C_BORDER, height=1).pack(fill="x", pady=(10, 8))

        # Fila de stats
        stats = tk.Frame(card, bg=C_WHITE)
        stats.pack(fill="x")

        def stat(parent, label, valor, color=C_TEXT_MUTED):
            f = tk.Frame(parent, bg=C_WHITE)
            f.pack(side="left", padx=(0, 24))
            tk.Label(f, text=label, font=("Segoe UI", 7),
                     bg=C_WHITE, fg=C_TEXT_MUTED).pack(anchor="w")
            tk.Label(f, text=valor, font=("Segoe UI", 9, "bold"),
                     bg=C_WHITE, fg=color).pack(anchor="w")

        stat(stats, "DOCUMENTOS",
             f"{exp['descargados']} / {exp['total']}")

        if exp["errores"] > 0:
            stat(stats, "ERRORES", str(exp["errores"]), C_LOG_ERR)

        stat(stats, "TAMAÑO", f"{exp['tam_mb']:.0f} MB")

        ult = exp["ultima_act"]
        if ult:
            try:
                dt = datetime.fromisoformat(ult)
                ult_fmt = dt.strftime("%d/%m/%Y %H:%M")
            except Exception:
                ult_fmt = ult[:16]
            stat(stats, "ÚLTIMA ACTUALIZACIÓN", ult_fmt)

        if exp.get("cid"):
            stat(stats, "CID", exp["cid"])

        # URL (truncada)
        if exp.get("url"):
            url_lbl = tk.Label(card, text=exp["url"],
                               font=("Segoe UI", 7),
                               bg=C_WHITE, fg=C_TEXT_MUTED,
                               cursor="hand2", anchor="w")
            url_lbl.pack(anchor="w", pady=(4, 0))
            url_lbl.bind("<Button-1>",
                         lambda e, u=exp["url"]: self._abrir_url(u))

    # ── Acciones ──────────────────────────────────────────────────

    def _abrir_carpeta(self, carpeta):
        import subprocess as _sp, platform as _pl
        try:
            s = _pl.system()
            if s == "Windows":
                _sp.Popen(["explorer", str(carpeta)])
            elif s == "Darwin":
                _sp.Popen(["open", str(carpeta)])
            else:
                _sp.Popen(["xdg-open", str(carpeta)])
        except Exception as e:
            self.app.set_status(f"No se pudo abrir la carpeta: {e}")

    def _abrir_url(self, url):
        import webbrowser
        webbrowser.open(url)

    def _actualizar(self, exp: dict):
        """Lanza una descarga solo de documentos nuevos para este expediente."""
        class Args:
            url          = exp["url"]
            carpeta      = exp["carpeta"]
            concurrentes = None
            reintentos   = None
            dry_run      = False
            jurisdiccion = exp.get("jurisdiccion", "")
            numero       = exp.get("numero", "")
            anio         = exp.get("anio", "")

        def _after():
            self.after(500, self.refrescar)

        self.app.run_task(scw.cmd_descargar, Args(), callback=_after)

    def _ir_a_descargar(self, exp: dict):
        """Navega a la pestana Descargar y precarga los datos del expediente."""
        page_descargar = self.app._pages.get("descargar")
        if page_descargar:
            page_descargar.precargar_expediente(exp)
        self.app._show("descargar")

    def _renombrar(self, exp: dict):
        """Muestra un diálogo para cambiar el nombre visible del expediente."""
        import json as _json

        dlg = tk.Toplevel(self)
        dlg.title("Renombrar expediente")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.configure(bg=C_WHITE)

        dlg.update_idletasks()
        x = self.winfo_rootx() + self.winfo_width() // 2 - 220
        y = self.winfo_rooty() + self.winfo_height() // 2 - 80
        dlg.geometry(f"440x160+{x}+{y}")

        tk.Label(dlg, text="✏  Renombrar expediente",
                 font=FONT_UI_BOLD, bg=C_WHITE, fg=C_TEXT,
                 anchor="w").pack(anchor="w", padx=18, pady=(16, 4))

        tk.Frame(dlg, bg=C_BORDER, height=1).pack(fill="x", padx=18)

        tk.Label(dlg, text="Nuevo nombre:",
                 font=FONT_UI, bg=C_WHITE, fg=C_TEXT_MUTED,
                 anchor="w").pack(anchor="w", padx=18, pady=(12, 2))

        var = tk.StringVar(value=exp["nombre"])
        ent = tk.Entry(dlg, textvariable=var, width=52,
                       font=FONT_UI, relief="solid", bd=1,
                       bg=C_WHITE, fg=C_TEXT,
                       highlightthickness=1,
                       highlightbackground=C_BORDER,
                       highlightcolor=C_ACCENT)
        ent.pack(padx=18, fill="x")
        ent.select_range(0, "end")
        ent.focus_set()

        msg_lbl = tk.Label(dlg, text="", font=("Segoe UI", 7),
                           bg=C_WHITE, fg=C_LOG_ERR)
        msg_lbl.pack(anchor="w", padx=18)

        def _guardar():
            nuevo = var.get().strip()
            if not nuevo:
                msg_lbl.config(text="El nombre no puede estar vacío.")
                return
            reg_path = exp["carpeta"] / "_registro.json"
            try:
                reg = _json.loads(reg_path.read_text(encoding="utf-8"))
                reg["nombre"] = nuevo
                reg_path.write_text(
                    _json.dumps(reg, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            except Exception as e:
                msg_lbl.config(text=f"Error al guardar: {e}")
                return
            dlg.destroy()
            self.after(100, self.refrescar)
            self.app.set_status(f"✅  Expediente renombrado a: {nuevo}")

        btn_bar = tk.Frame(dlg, bg=C_WHITE)
        btn_bar.pack(anchor="e", padx=18, pady=(8, 14))
        _btn(btn_bar, "Cancelar", dlg.destroy,
             style="ghost").pack(side="left", padx=(0, 8))
        _btn(btn_bar, "Guardar", _guardar,
             style="accent").pack(side="left")

        dlg.bind("<Return>", lambda _: _guardar())
        dlg.bind("<Escape>", lambda _: dlg.destroy())


# ─── Ayuda ────────────────────────────────────────────────────────

class PageAyuda(BasePage):
    """Guía de uso integrada en la aplicación."""

    # ── Contenido de la guía ──────────────────────────────────────
    _SECCIONES = [
        {
            "titulo": "¿Qué hace este programa?",
            "icono": "⚖",
            "items": [
                ("", (
                    "SCW es una herramienta para descargar, unir y organizar los documentos "
                    "de un expediente judicial del Sistema de Consulta Web del Poder Judicial "
                    "de la Nación (scw.pjn.gov.ar).\n\n"
                    "El flujo típico es: descargar todos los PDFs → unirlos en un solo archivo "
                    "→ dividir ese archivo por año para manejarlo más fácilmente."
                )),
            ]
        },
        {
            "titulo": "Requisitos previos",
            "icono": "📋",
            "items": [
                ("Python 3.10 o superior",
                 "Necesario para ejecutar el programa.\n"
                 "Descargalo desde python.org si no lo tenés instalado."),
                ("Dependencias Python",
                 "Abrí una terminal en la carpeta del proyecto y ejecutá:\n"
                 "  pip install -r requirements.txt\n\n"
                 "Esto instala: pypdf, reportlab, playwright, httpx."),
                ("Navegador Chromium (para descargar)",
                 "Después de instalar las dependencias, ejecutá:\n"
                 "  playwright install chromium\n\n"
                 "Solo se necesita una vez. Instala un navegador interno que "
                 "usa el programa para acceder al expediente."),
                ("Ghostscript (opcional, para comprimir)",
                 "Descargalo desde ghostscript.com/download/gsdnld.html\n"
                 "Sin él, la unión funciona igual pero sin comprimir los PDFs.\n"
                 "La sección 'Unir PDFs' muestra si está instalado o no."),
            ]
        },
        {
            "titulo": "Paso 1 — Descargar PDFs",
            "icono": "⬇",
            "items": [
                ("Obtener la URL del expediente",
                 "Abrí el expediente en tu navegador en scw.pjn.gov.ar.\n"
                 "Copiá la dirección completa de la barra de direcciones\n"
                 "(tiene la forma: .../expediente.seam?cid=XXXXX)\n"
                 "y pegala en el campo 'URL del expediente'."),
                ("Iniciar la descarga",
                 "Hacé clic en '▶ Ejecutar descarga'.\n"
                 "Se va a abrir una ventana del navegador automáticamente.\n"
                 "Si el sitio pide login, iniciá sesión en esa ventana y\n"
                 "el programa va a continuar solo."),
                ("Seguir el progreso",
                 "El log de abajo muestra cada página scrapeada y\n"
                 "la barra de progreso de las descargas en tiempo real.\n"
                 "Los PDFs se guardan en la carpeta configurada."),
                ("Si algo falló",
                 "Los archivos con error quedan anotados en _errores.txt\n"
                 "dentro de la carpeta de descarga.\n"
                 "Volvé a ejecutar la descarga: los que ya existen se saltean\n"
                 "y solo se reintentan los que faltaron."),
            ]
        },
        {
            "titulo": "Paso 2 — Unir PDFs",
            "icono": "📎",
            "items": [
                ("¿Qué hace este paso?",
                 "Toma todos los PDFs descargados y los une en un único\n"
                 "archivo, insertando una página separadora entre cada uno\n"
                 "con la fecha, tipo y descripción del documento."),
                ("Calidad de compresión",
                 "• screen  → archivo pequeño, para compartir por mail\n"
                 "• ebook   → equilibrio recomendado (default)\n"
                 "• printer → alta calidad, para imprimir\n"
                 "Usá el botón ? al lado del combo para más detalles."),
                ("Sin comprimir",
                 "Si Ghostscript no está instalado o querés velocidad,\n"
                 "tildá 'Sin comprimir'. El resultado es igual pero más grande."),
            ]
        },
        {
            "titulo": "Paso 3 — Dividir por año",
            "icono": "✂",
            "items": [
                ("¿Qué hace este paso?",
                 "Divide el PDF unificado separándolo por año de los documentos.\n"
                 "Si un año tiene muchos documentos y supera el límite de MB\n"
                 "configurado, lo divide en partes: 2022_parte1, 2022_parte2..."),
                ("Tamaño máximo",
                 "40 MB es un buen valor para adjuntar en correos o sistemas\n"
                 "judiciales. Bajalo a 25 si tenés restricciones más estrictas."),
                ("Exportar un solo año",
                 "Si completás el campo 'Solo un año' (ej: 2023), solo se\n"
                 "genera el PDF de ese año. Útil para buscar algo puntual."),
            ]
        },
        {
            "titulo": "Flujo completo (atajo)",
            "icono": "🔄",
            "items": [
                ("", (
                    "La sección 'Flujo completo' ejecuta los 3 pasos en secuencia\n"
                    "con un solo clic, usando los parámetros guardados en config.ini.\n\n"
                    "Recomendado una vez que ya configuraste todo y querés\n"
                    "procesar un expediente de principio a fin."
                )),
            ]
        },
        {
            "titulo": "Dry-run (modo simulación)",
            "icono": "🔍",
            "items": [
                ("", (
                    "Disponible en todas las secciones. Cuando está activado,\n"
                    "el programa recorre todo el proceso y muestra en el log\n"
                    "exactamente qué haría, pero SIN descargar ni crear archivos.\n\n"
                    "Muy útil para verificar la URL, ver cuántos documentos hay,\n"
                    "o estimar cuánto tiempo va a llevar antes de ejecutar de verdad."
                )),
            ]
        },
        {
            "titulo": "Guardar configuración",
            "icono": "💾",
            "items": [
                ("", (
                    "Cada sección tiene un botón 'Guardar en config.ini'.\n"
                    "Al guardarlo, la próxima vez que abras el programa los\n"
                    "valores van a estar precargados.\n\n"
                    "También podés editar config.ini directamente con\n"
                    "cualquier editor de texto (Notepad, VS Code, etc.)."
                )),
            ]
        },
        {
            "titulo": "Solución de problemas",
            "icono": "🔧",
            "items": [
                ("La ventana del navegador no abre",
                 "Verificá que ejecutaste: playwright install chromium\n"
                 "Si sigue sin funcionar, reinstalá con:\n"
                 "pip install --upgrade playwright && playwright install chromium"),
                ("La tabla del expediente no carga",
                 "El sitio puede requerir que inicies sesión.\n"
                 "Cuando se abra el navegador, iniciá sesión manualmente\n"
                 "y el programa va a continuar automáticamente."),
                ("Faltan PDFs al terminar",
                 "Aumentá la 'Pausa entre páginas' a 6 o más segundos.\n"
                 "El servidor puede ser lento y la tabla no termina de cargar."),
                ("Ghostscript no encontrado",
                 "Descargá el instalador de 64 bits desde:\n"
                 "ghostscript.com/download/gsdnld.html\n"
                 "Reiniciá el programa después de instalarlo."),
                ("Error al importar módulos",
                 "Ejecutá en la carpeta del proyecto:\n"
                 "pip install -r requirements.txt\n"
                 "Si persiste, verificá que Python sea 3.10 o superior."),
            ]
        },
    ]

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self._build_header("📖", "Guía de uso",
                           "Todo lo que necesitás saber para usar el programa")
        self._build_content()

    def _build_content(self):
        for sec in self._SECCIONES:
            self._render_seccion(self._body, sec)
        tk.Frame(self._body, bg=C_CONTENT_BG, height=16).pack()

    def _render_seccion(self, parent, sec: dict):
        outer = tk.Frame(parent, bg=C_CONTENT_BG)
        outer.pack(fill="x", padx=14, pady=(10, 0))

        # Encabezado de sección
        hdr = tk.Frame(outer, bg=C_CONTENT_BG)
        hdr.pack(fill="x", pady=(0, 8))
        tk.Label(hdr,
                 text=f"{sec['icono']}  {sec['titulo']}",
                 font=("Segoe UI", 10, "bold"),
                 bg=C_CONTENT_BG, fg=C_TEXT).pack(side="left")
        tk.Frame(hdr, bg=C_BORDER, height=1).pack(
            side="left", fill="x", expand=True, padx=(10, 0), pady=4)

        # Items de la sección
        for subtitulo, texto in sec["items"]:
            card = tk.Frame(outer, bg=C_WHITE,
                            highlightbackground=C_BORDER,
                            highlightthickness=1,
                            padx=16, pady=10)
            card.pack(fill="x", pady=(0, 6))

            if subtitulo:
                tk.Label(card, text=subtitulo,
                         font=("Segoe UI", 9, "bold"),
                         bg=C_WHITE, fg=C_ACCENT,
                         anchor="w").pack(anchor="w", pady=(0, 4))

            tk.Label(card, text=texto,
                     font=("Segoe UI", 9),
                     bg=C_WHITE, fg=C_TEXT,
                     anchor="w", justify="left",
                     wraplength=700).pack(anchor="w")


# ══════════════════════════════════════════════════════════════════
#  APLICACIÓN PRINCIPAL
# ══════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SCW – Expedientes Judiciales")
        self.geometry("1080x760")
        self.minsize(820, 560)
        self.configure(bg=C_CONTENT_BG)

        self._task_running = False

        # Icono en la barra de tareas (sin archivo externo)
        try:
            self.iconbitmap(default="")
        except Exception:
            pass

        # Redirigir stdout al widget de log
        sys.stdout = _QueueStream(_log_queue)

        self._build_ui()
        self._poll_queue()

        if not _SCW_OK:
            messagebox.showerror(
                "Error de importación",
                "No se pudo importar pjn_scw.cli.\n"
                "Ejecutá desde la raíz del proyecto (donde está la carpeta pjn_scw)."
            )

    # ─── Layout ───────────────────────────────────────────────────

    def _build_ui(self):
        # Contenedor principal: sidebar izquierdo + área derecha
        main = tk.Frame(self, bg=C_CONTENT_BG)
        main.pack(fill="both", expand=True)

        # ── Barra lateral ─────────────────────────────────────────
        self._sidebar = tk.Frame(main, bg=C_SIDEBAR_BG, width=190)
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)

        # Logo / branding
        logo_frame = tk.Frame(self._sidebar, bg=C_SIDEBAR_BG, pady=20)
        logo_frame.pack(fill="x")
        tk.Label(logo_frame, text="⚖", font=("Segoe UI", 22),
                 bg=C_SIDEBAR_BG, fg="#ffffff").pack()
        tk.Label(logo_frame, text="SCW", font=("Segoe UI", 13, "bold"),
                 bg=C_SIDEBAR_BG, fg="#ffffff").pack()
        tk.Label(logo_frame, text="Expedientes PJN",
                 font=("Segoe UI", 8),
                 bg=C_SIDEBAR_BG, fg=C_SIDEBAR_MUTED).pack(pady=(2, 0))

        tk.Frame(self._sidebar, bg=C_SIDEBAR_SEL,
                 height=1).pack(fill="x", padx=16, pady=(0, 12))

        # ── Contenedor de páginas ─────────────────────────────────
        right = tk.Frame(main, bg=C_CONTENT_BG)
        right.pack(side="left", fill="both", expand=True)

        # Área de páginas (top)
        self._page_area = tk.Frame(right, bg=C_CONTENT_BG)
        self._page_area.pack(fill="both", expand=True)

        # Panel inferior (PanedWindow para que sea redimensionable)
        paned = tk.PanedWindow(right, orient="vertical",
                               sashrelief="flat", sashwidth=4,
                               bg=C_BORDER)
        paned.pack(fill="x", side="bottom")

        # Área de log
        log_container = tk.Frame(right, bg=C_LOG_BG, height=160)
        log_container.pack(fill="x", side="bottom")
        log_container.pack_propagate(False)
        self._build_log(log_container)

        # Barra de estado
        status_bar = tk.Frame(right, bg=C_SIDEBAR_BG, height=24)
        status_bar.pack(fill="x", side="bottom")
        status_bar.pack_propagate(False)

        self._progress = ttk.Progressbar(status_bar, mode="indeterminate",
                                         length=120)
        self._progress.pack(side="right", padx=10, pady=4)

        self._status_lbl = tk.Label(
            status_bar, text="  Listo.", font=("Segoe UI", 8),
            bg=C_SIDEBAR_BG, fg=C_SIDEBAR_FG, anchor="w")
        self._status_lbl.pack(side="left", fill="x", expand=True)

        # ── Páginas y botones de navegación ──────────────────────
        self._pages: dict[str, BasePage] = {}
        self._nav_btns: dict[str, tk.Button] = {}

        nav = [
            ("📂  Expedientes",     "expedientes", PageExpedientes),
            ("⬇   Descargar",      "descargar",   PageDescargar),
            ("📎  Unir PDFs",       "unir",        PageUnir),
            ("✂   Dividir",        "dividir",     PageDividir),
            ("🔄  Flujo completo",  "todo",        PageTodo),
            ("📖  Guía de uso",     "ayuda",       PageAyuda),
        ]

        for label, key, PageCls in nav:
            page = PageCls(self._page_area, self)
            page.place(relx=0, rely=0, relwidth=1, relheight=1)
            self._pages[key] = page

            btn = tk.Button(
                self._sidebar, text=f"  {label}",
                anchor="w", font=FONT_SIDEBAR,
                bg=C_SIDEBAR_BG, fg=C_SIDEBAR_FG,
                activebackground=C_SIDEBAR_HVR,
                activeforeground="#ffffff",
                relief="flat", bd=0,
                padx=6, pady=10,
                cursor="hand2",
                command=lambda k=key: self._show(k)
            )
            btn.pack(fill="x")
            self._nav_btns[key] = btn

        # Separador + versión al final del sidebar
        tk.Frame(self._sidebar, bg=C_SIDEBAR_SEL,
                 height=1).pack(fill="x", padx=16, pady=(12, 8), side="bottom")
        tk.Label(self._sidebar, text="SCW v1.1",
                 font=("Segoe UI", 7), bg=C_SIDEBAR_BG,
                 fg=C_SIDEBAR_MUTED).pack(side="bottom", pady=(0, 8))

        self._show("expedientes")

    def _build_log(self, container: tk.Frame):
        # Encabezado del log
        hdr = tk.Frame(container, bg="#161b22", height=26)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        tk.Label(hdr, text="  📋 Log de actividad",
                 font=("Segoe UI", 8, "bold"),
                 bg="#161b22", fg="#8b949e").pack(side="left", padx=4, pady=5)

        tk.Button(hdr, text="Limpiar log",
                  bg="#161b22", fg="#8b949e",
                  relief="flat", bd=0, font=("Segoe UI", 8),
                  cursor="hand2", activebackground="#21262d",
                  activeforeground="#c9d1d9",
                  command=self._clear_log).pack(side="right", padx=8, pady=3)

        # Text widget
        text_frame = tk.Frame(container, bg=C_LOG_BG)
        text_frame.pack(fill="both", expand=True)

        self._log = tk.Text(
            text_frame,
            bg=C_LOG_BG, fg=C_LOG_FG,
            font=FONT_MONO, wrap="word",
            state="disabled", relief="flat",
            insertbackground="white",
            selectbackground="#264f78",
            padx=8, pady=6,
            bd=0
        )
        vsb = tk.Scrollbar(text_frame, command=self._log.yview,
                           bg=C_LOG_BG, troughcolor=C_LOG_BG,
                           relief="flat")
        self._log.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._log.pack(fill="both", expand=True)

        # Tags de color para distintos tipos de mensaje
        self._log.tag_configure("ok",   foreground=C_LOG_OK)
        self._log.tag_configure("err",  foreground=C_LOG_ERR)
        self._log.tag_configure("warn", foreground=C_LOG_WARN)
        self._log.tag_configure("info", foreground=C_LOG_INFO)
        self._log.tag_configure("dim",  foreground="#484f58")

    # ─── Navegación ───────────────────────────────────────────────

    def _show(self, key: str):
        for k, page in self._pages.items():
            page.place_forget()
        self._pages[key].place(relx=0, rely=0, relwidth=1, relheight=1)

        for k, btn in self._nav_btns.items():
            if k == key:
                btn.config(bg=C_SIDEBAR_SEL, fg="#ffffff")
            else:
                btn.config(bg=C_SIDEBAR_BG, fg=C_SIDEBAR_FG)

    # ─── Log ──────────────────────────────────────────────────────

    def _clear_log(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    def _append_log(self, text: str):
        """Inserta texto en el log, manejando \\r para barras de progreso."""
        self._log.configure(state="normal")

        # Dividir por \r y \n para manejar progress bars
        for chunk in text.split("\r"):
            if chunk == "":
                continue
            if "\n" in chunk:
                self._log.insert("end", chunk)
            else:
                # Sobreescribir la última línea (comportamiento de \r)
                idx = self._log.index("end-1c linestart")
                self._log.delete(idx, "end")
                self._log.insert("end", chunk)

        # Colorear según contenido
        last_line = self._log.get("end-2l", "end")
        tag = None
        if any(w in last_line for w in ["✓", "OK", "completo", "✅"]):
            tag = "ok"
        elif any(w in last_line for w in ["ERROR", "Error", "❌", "FALLA"]):
            tag = "err"
        elif any(w in last_line for w in ["WARN", "⚠", "WARNING"]):
            tag = "warn"
        elif any(w in last_line for w in ["INFO", "iniciado", "Inicio"]):
            tag = "info"

        if tag:
            self._log.tag_add(tag, "end-2l", "end-1c")

        self._log.see("end")
        self._log.configure(state="disabled")

    def _poll_queue(self):
        """Drena la cola de log cada 80 ms."""
        try:
            while True:
                text = _log_queue.get_nowait()
                self._append_log(text)
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    # ─── Ejecución de tareas ──────────────────────────────────────

    def run_task(self, func, args, callback=None):
        if self._task_running:
            messagebox.showwarning(
                "Tarea en curso",
                "Hay una operación en ejecución.\n"
                "Esperá a que termine antes de iniciar otra."
            )
            return

        if not _SCW_OK:
            messagebox.showerror("Error", "El módulo pjn_scw.cli no está disponible.")
            return

        self._task_running = True
        self._progress.start(10)
        self.set_status("⏳  Ejecutando…")

        def _worker():
            try:
                func(args)
            except SystemExit:
                pass
            except Exception as exc:
                _log_queue.put(f"\n❌ Error inesperado: {exc}\n")
            finally:
                self.after(0, lambda: self._task_done(callback))

        threading.Thread(target=_worker, daemon=True).start()

    def _task_done(self, callback=None):
        self._task_running = False
        self._progress.stop()
        self.set_status("✅  Listo.")
        if callback:
            callback()

    def set_status(self, msg: str):
        self._status_lbl.config(text=f"  {msg}")


# ══════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════

def main():
    app = App()
    app.mainloop()
    sys.stdout = sys.__stdout__  # restaurar stdout al cerrar


if __name__ == "__main__":
    main()
