# SCW – Herramienta de Expedientes Judiciales

Herramienta con **interfaz gráfica** para descargar, unir y dividir los PDFs de un expediente del Sistema de Consulta Web del Poder Judicial de la Nación ([scw.pjn.gov.ar](https://scw.pjn.gov.ar)).

---

## Capturas

| Descargar | Unir PDFs | Guía de uso |
|-----------|-----------|-------------|
| Configura URL, descargas paralelas y reintentos | Detecta Ghostscript y permite configurar calidad | Guía integrada con todos los pasos |

---

## Instalación

### 1. Clonar el repositorio
```bash
git clone https://github.com/tu-usuario/scw.git
cd scw
```

### 2. Instalar dependencias Python
```bash
pip install -r requirements.txt
```

### 3. Instalar el navegador de Playwright
```bash
playwright install chromium
```

### 4. Instalar Ghostscript *(opcional — necesario para comprimir PDFs al unir)*

| Sistema | Instrucción |
|---------|-------------|
| Windows | Descargar desde [ghostscript.com/download/gsdnld.html](https://www.ghostscript.com/download/gsdnld.html) |
| macOS   | `brew install ghostscript` |
| Linux   | `sudo apt install ghostscript` |

---

## Uso

Ejecutá los comandos desde la **carpeta raíz del repositorio** (donde está el paquete `pjn_scw/`), para que Python resuelva el módulo correctamente.

### Interfaz gráfica (recomendado)
```bash
python -m pjn_scw.gui
```
Abre la aplicación con panel lateral de navegación, tooltips explicativos en cada campo, detección automática de Ghostscript y log en tiempo real.

### Línea de comandos
```bash
python -m pjn_scw.cli                        # menú interactivo
python -m pjn_scw.cli descargar              # solo descargar PDFs
python -m pjn_scw.cli unir                   # solo unir PDFs
python -m pjn_scw.cli dividir                # solo dividir por año
python -m pjn_scw.cli todo                   # flujo completo (1+2+3)
python -m pjn_scw.cli estado                 # resumen de archivos
```

### Opciones principales

```bash
# Descargar
python -m pjn_scw.cli descargar --url "https://scw.pjn.gov.ar/scw/expediente.seam?cid=XXXXX"
python -m pjn_scw.cli descargar --concurrentes 12 --reintentos 5

# Unir
python -m pjn_scw.cli unir --calidad screen      # mínimo peso
python -m pjn_scw.cli unir --calidad ebook       # equilibrado (default)
python -m pjn_scw.cli unir --calidad printer     # alta calidad
python -m pjn_scw.cli unir --sin-comprimir

# Dividir
python -m pjn_scw.cli dividir --max-mb 25
python -m pjn_scw.cli dividir --solo-año 2023

# Simular sin ejecutar nada
python -m pjn_scw.cli todo --dry-run
```

---

## Flujo típico

```
1. Obtener la URL del expediente desde scw.pjn.gov.ar
2. Configurar la URL en la pestaña "Descargar" y ejecutar
3. Una vez descargados todos los PDFs → pestaña "Unir PDFs"
4. Con el PDF unificado → pestaña "Dividir por año"
```

O todo de una vez:
```bash
python -m pjn_scw.cli todo
```

---

## Estructura del proyecto

```
.
├── pjn_scw/            ← paquete (ejecutar: python -m pjn_scw.cli / pjn_scw.gui)
│   ├── cli.py          ← descarga, unión, división, menú interactivo
│   └── gui.py          ← interfaz gráfica (tkinter + stdlib)
├── instalacion/        ← Windows: PyInstaller + Inno Setup
│   ├── construir_instalador.bat
│   ├── scw_setup.iss
│   └── assets/         ← icon.ico (opcional) para el .exe y el instalador
├── crear_registro.py   ← utilidad: regenerar _registro.json
├── config.ini          ← configuración central (editable)
├── requirements.txt    ← dependencias Python
├── README.md
│
├── expediente_pdfs/            ← PDFs descargados (se crea automáticamente)
│   └── _errores.txt            ← PDFs que fallaron (si los hay)
├── expediente_unificado.pdf    ← PDF unificado (se crea al unir)
└── expediente_por_año/         ← PDFs divididos (se crea al dividir)
    ├── expediente_2021.pdf
    ├── expediente_2022_parte1.pdf
    └── expediente_2022_parte2.pdf
```

Para generar el `.exe` y el instalador en Windows, ejecutá **`instalacion\construir_instalador.bat`** (hace `cd` a la raíz del repo solo). El instalador compilado queda en **`instalador\SCW_Setup.exe`**.

Colocá el icono en **`instalacion/assets/icon.ico`** si querés icono en el `.exe` y en el wizard de Inno Setup; si no existe, el empaquetado sigue sin icono personalizado.

---

## Configuración (`config.ini`)

Todos los parámetros tienen valores por defecto. Solo modificá lo que necesitás:

| Sección | Parámetro | Default | Descripción |
|---------|-----------|---------|-------------|
| `[descarga]` | `url_default` | — | URL del expediente |
| `[descarga]` | `max_concurrentes` | 8 | Descargas en paralelo |
| `[descarga]` | `pausa_pagina` | 4.0 | Segundos de espera entre páginas AJAX |
| `[descarga]` | `reintentos` | 3 | Reintentos por PDF fallido |
| `[union]` | `calidad` | ebook | Calidad Ghostscript (screen/ebook/printer/prepress) |
| `[union]` | `workers` | núcleos/2 | Procesos de compresión en paralelo |
| `[division]` | `max_mb` | 40 | Tamaño máximo por archivo en MB |
| `[division]` | `solo_año` | *(vacío)* | Filtrar un año específico al dividir |

---

## Log de actividad

Cada ejecución queda registrada en `scw.log`:
```
10:32:14  INFO     Descarga iniciada — URL: https://scw...
10:35:02  INFO     Scraping completo: 347 PDFs
10:58:41  INFO     Descarga completa — OK:345  Errores:2
```

---

## Solución de problemas

| Problema | Solución |
|----------|----------|
| `ModuleNotFoundError` | `pip install -r requirements.txt` |
| Playwright no instalado | `pip install playwright && playwright install chromium` |
| La tabla del expediente no carga | Iniciá sesión manualmente en el navegador que abre el programa |
| Faltan PDFs al terminar | Aumentá `pausa_pagina` a 6 o más segundos |
| Ghostscript no encontrado | Instalarlo o usar `--sin-comprimir` / tildar "Sin comprimir" en la GUI |
| PDF final muy grande | Usar calidad `screen` al unir |

---

## Requisitos

- Python 3.10+
- Ver `requirements.txt` para dependencias Python
- Ghostscript (opcional, para compresión)

---

## Licencia

MIT
