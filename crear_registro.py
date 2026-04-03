#!/usr/bin/env python3
"""
crear_registro.py — Crea el _registro.json faltante para una carpeta
de PDFs que fue movida/creada manualmente, para que aparezca en la
pestaña Expedientes de la GUI.

Uso:
    python crear_registro.py
    python crear_registro.py "expedientes/Mi Expediente"
    python crear_registro.py "C:\\Usuarios\\yo\\expedientes\\9468-2020"
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path


def crear_registro(carpeta: Path, url: str = "", nombre: str = ""):
    carpeta = Path(carpeta).resolve()

    if not carpeta.exists():
        print(f"❌  La carpeta no existe: {carpeta}")
        return

    pdfs = sorted(carpeta.glob("*.pdf"))
    if not pdfs:
        print(f"⚠   No hay PDFs en: {carpeta}")
        print("    Verificá que moviste los archivos a la carpeta correcta.")
        return

    reg_path = carpeta / "_registro.json"
    if reg_path.exists():
        print(f"ℹ   Ya existe un _registro.json en: {carpeta}")
        print("    Borralo primero si querés regenerarlo.")
        return

    # Nombre: usar el de la carpeta si no se pasó
    if not nombre:
        nombre = carpeta.name

    # Intentar extraer CID de la URL
    cid = ""
    if url:
        m = re.search(r'cid=(\d+)', url)
        if m:
            cid = m.group(1)

    # Construir entradas de documentos a partir de los nombres de archivo
    documentos = {}
    patron_fecha = re.compile(r'^(\d{4}-\d{2}-\d{2})')
    for pdf in pdfs:
        nombre_doc = pdf.stem
        fecha_doc = ""
        m = patron_fecha.match(nombre_doc)
        if m:
            fecha_doc = m.group(1)

        documentos[nombre_doc] = {
            "url":            "",
            "fecha_doc":      fecha_doc,
            "tipo":           "",
            "detalle":        "",
            "descargado_en":  datetime.now().isoformat(timespec="seconds"),
            "tamano_bytes":   pdf.stat().st_size,
        }

    ahora = datetime.now().isoformat(timespec="seconds")
    registro = {
        "nombre":               nombre,
        "url":                  url,
        "cid":                  cid,
        "primera_descarga":     ahora,
        "ultima_actualizacion": ahora,
        "total_documentos":     len(pdfs),
        "documentos":           documentos,
    }

    reg_path.write_text(
        json.dumps(registro, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print(f"✅  _registro.json creado en: {carpeta}")
    print(f"    Nombre    : {nombre}")
    print(f"    Documentos: {len(pdfs)} PDFs registrados")
    if url:
        print(f"    URL       : {url}")
    print()
    print("Ahora abrí (o refrescá) la GUI y el expediente va a aparecer")
    print("en la pestaña 📂 Expedientes.")


def main():
    if len(sys.argv) > 1:
        carpeta = Path(sys.argv[1])
        url = sys.argv[2] if len(sys.argv) > 2 else ""
        crear_registro(carpeta, url)
        return

    print("=" * 60)
    print("  Crear _registro.json para expediente movido manualmente")
    print("=" * 60)
    print()
    print("Este script registra una carpeta de PDFs para que aparezca")
    print("en la pestaña Expedientes de la GUI.")
    print()

    carpeta_str = input("  Ruta de la carpeta con los PDFs\n  > ").strip().strip('"')
    if not carpeta_str:
        print("❌  No ingresaste ninguna ruta.")
        return

    carpeta = Path(carpeta_str)

    url = input("\n  URL del expediente (Enter para omitir)\n  > ").strip()
    nombre = input("\n  Nombre del expediente (Enter = usar nombre de carpeta)\n  > ").strip()

    print()
    crear_registro(carpeta, url, nombre)


if __name__ == "__main__":
    main()
