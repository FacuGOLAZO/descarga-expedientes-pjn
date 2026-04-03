; scw_setup.iss — Script de Inno Setup para SCW
; Ubicación: instalacion\  (rutas relativas al .iss)
; Salida: ..\instalador\SCW_Setup.exe

#define AppName      "SCW - Expedientes Judiciales"
#define AppVersion   "1.1"
#define AppPublisher "SCW"
#define AppExeName   "SCW.exe"
#define SourceDir    "..\dist\SCW"

[Setup]
AppId={{B7E2A3C1-4F92-4D8E-9A1B-3C6D5E8F0A2B}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisherURL=https://scw.pjn.gov.ar
AppSupportURL=https://scw.pjn.gov.ar
DefaultDirName={autopf}\SCW
DefaultGroupName={#AppName}
AllowNoIcons=yes
OutputDir=..\instalador
OutputBaseFilename=SCW_Setup
#if FileExists("assets\icon.ico")
SetupIconFile=assets\icon.ico
#endif
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#AppExeName}
; Requiere Windows 10 o superior
MinVersion=10.0

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear acceso directo en el Escritorio"; GroupDescription: "Opciones adicionales:"; Flags: unchecked

[Files]
; Toda la carpeta generada por PyInstaller (en la raíz del repo)
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Menú inicio
Name: "{group}\{#AppName}";        Filename: "{app}\{#AppExeName}"
Name: "{group}\Desinstalar SCW";   Filename: "{uninstallexe}"
; Escritorio (opcional)
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Ofrecer abrir la app al terminar la instalación
Filename: "{app}\{#AppExeName}"; \
    Description: "Abrir SCW ahora"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Limpiar archivos generados por la app (logs, configs locales)
Type: filesandordirs; Name: "{app}\expediente_pdfs"
Type: filesandordirs; Name: "{app}\expediente_por_año"
Type: files;          Name: "{app}\expediente_unificado.pdf"
Type: files;          Name: "{app}\scw.log"
