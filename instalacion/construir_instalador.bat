@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0\.."
title SCW - Construir Instalador

echo.
echo ============================================================
echo   SCW - Generador de instalador para Windows
echo ============================================================
echo.

if not exist "pjn_scw\gui.py" (
    echo [ERROR] No se encontro pjn_scw\gui.py.
    echo         Este script debe estar en instalacion\ y el repo completo arriba.
    pause
    exit /b 1
)
if not exist "pjn_scw\cli.py" (
    echo [ERROR] No se encontro el paquete pjn_scw (pjn_scw\cli.py).
    pause
    exit /b 1
)

echo [1/8] Verificando Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python no esta instalado o no esta en el PATH.
    echo         Descargalo desde https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo        %%v encontrado.

echo.
echo [2/8] Instalando PyInstaller...
pip install --quiet --upgrade pyinstaller
if errorlevel 1 (
    echo [ERROR] No se pudo instalar PyInstaller.
    pause
    exit /b 1
)
echo        PyInstaller listo.

echo.
echo [3/8] Instalando dependencias del proyecto...
pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Fallo pip install -r requirements.txt
    pause
    exit /b 1
)
echo        Dependencias listas.

echo.
echo [4/8] Instalando Chromium para Playwright...
echo        Los binarios van a playwright-browsers\ (reproducible, no al perfil de usuario^)
set "PLAYWRIGHT_BROWSERS_PATH=%CD%\playwright-browsers"
echo        (puede tardar unos minutos la primera vez)
playwright install chromium
if errorlevel 1 (
    echo [ERROR] No se pudo instalar Chromium. Corregi el error y volve a ejecutar este script.
    pause
    exit /b 1
)
echo        Chromium listo.

echo.
echo [5/8] Empaquetando con PyInstaller...
if exist "dist\SCW" rmdir /s /q "dist\SCW"
if exist "build"    rmdir /s /q "build"

for /f "tokens=*" %%p in ('python -c "import playwright, os; print(os.path.dirname(playwright.__file__))"') do set PLAYWRIGHT_PKG=%%p

set PYI_ICON=
if exist "instalacion\assets\icon.ico" set PYI_ICON=--icon "instalacion\assets\icon.ico"

pyinstaller --name "SCW" --windowed %PYI_ICON% --add-data "config.ini;." --add-data "%PLAYWRIGHT_PKG%;playwright" --hidden-import "playwright" --hidden-import "playwright.async_api" --hidden-import "pypdf" --hidden-import "reportlab" --hidden-import "httpx" --hidden-import "pjn_scw" --hidden-import "pjn_scw.cli" --hidden-import "pjn_scw.gui" --noconfirm pjn_scw\gui.py

if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller fallo. Revisa los mensajes de arriba.
    pause
    exit /b 1
)
echo        Empaquetado completado.

echo.
echo [6/8] Copiando Chromium al paquete ^(dist\SCW\browsers^)...
if not exist "playwright-browsers\*" (
    echo [ERROR] La carpeta playwright-browsers esta vacia. Paso 4 debio poblarla.
    pause
    exit /b 1
)
if not exist "dist\SCW\browsers" mkdir "dist\SCW\browsers"
robocopy "%CD%\playwright-browsers" "%CD%\dist\SCW\browsers" /E /NFL /NDL /NJH
if errorlevel 8 (
    echo [ERROR] robocopy fallo al copiar playwright-browsers.
    pause
    exit /b 1
)
where /r "dist\SCW\browsers" chrome.exe >nul 2>&1
if errorlevel 1 (
    echo [ERROR] No se encontro chrome.exe bajo dist\SCW\browsers. Revisa playwright install chromium.
    pause
    exit /b 1
)
echo        Chromium copiado ^(junto a SCW.exe, como espera la app empaquetada^).

echo.
echo [7/8] Preparando instalador de Ghostscript (opcional en el Setup)...
if not exist "instalacion\redist" mkdir "instalacion\redist"
if not exist "instalacion\redist\gs10070w64.exe" (
    echo        Descargando gs10070w64.exe (Ghostscript)...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10070/gs10070w64.exe' -OutFile 'instalacion/redist/gs10070w64.exe' -UseBasicParsing"
    if errorlevel 1 (
        echo [AVISO] No se pudo descargar Ghostscript. El instalador se generara igual, pero sin esa opcion.
    )
)
if exist "instalacion\redist\gs10070w64.exe" (
    echo        Ghostscript listo para incluirse en el Setup.
) else (
    echo        Ghostscript no disponible. Se omitira del Setup.
)

echo.
echo [8/8] Generando instalador con Inno Setup...

set INNO=
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set INNO=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
if exist "C:\Program Files\Inno Setup 6\ISCC.exe"       set INNO=C:\Program Files\Inno Setup 6\ISCC.exe

if "%INNO%"=="" (
    echo.
    echo [AVISO] Inno Setup no esta instalado.
    echo         El .exe ya fue generado en:  dist\SCW\SCW.exe
    echo         Para crear el instalador, descarga Inno Setup desde https://jrsoftware.org/isdl.php
    echo.
    pause
    exit /b 0
)

"%INNO%" "%~dp0scw_setup.iss"
if errorlevel 1 (
    echo [ERROR] Inno Setup fallo. Revisa instalacion\scw_setup.iss.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   LISTO - El instalador fue creado en: instalador\SCW_Setup.exe
echo ============================================================
echo.
pause
