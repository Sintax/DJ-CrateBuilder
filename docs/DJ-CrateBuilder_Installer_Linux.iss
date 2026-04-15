; ============================================================================
; DJ-CrateBuilder v1.2 — Inno Setup Installer (Linux / Wine)
; Installs to user Program Files with no admin required
; ============================================================================
;
; BEFORE COMPILING:
;   1. Generate a unique GUID at https://www.guidgenerator.com/
;      and replace the AppId value below.
;   2. Update SourceDir to point to your PyInstaller dist\DJ-CrateBuilder\ folder.
;   3. (Optional) Update SetupIconFile to your .ico path.
;
; LINUX USAGE:
;   wine DJ-CrateBuilder_v1.2_Setup_Linux.exe
;
; ============================================================================

[Setup]
; IMPORTANT: Replace this GUID with your own unique identifier
AppId={{PUT-YOUR-GENERATED-GUID-HERE}
AppName=DJ-CrateBuilder
AppVersion=1.2
AppVerName=DJ-CrateBuilder v1.2
AppPublisher=DJ-CrateBuilder
DefaultDirName={userpf}\DJ-CrateBuilder
DefaultGroupName=DJ-CrateBuilder
AllowNoIcons=yes
OutputDir=Output
OutputBaseFilename=DJ-CrateBuilder_v1.2_Setup_Linux
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; No admin/UAC for Wine compatibility
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
; Uncomment and set path to use a custom icon:
; SetupIconFile=C:\path\to\icon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; IMPORTANT: Update this path to your PyInstaller output folder
Source: "dist\DJ-CrateBuilder\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\DJ-CrateBuilder"; Filename: "{app}\DJ-CrateBuilder.exe"
Name: "{group}\Uninstall DJ-CrateBuilder"; Filename: "{uninstallexe}"
Name: "{autodesktop}\DJ-CrateBuilder"; Filename: "{app}\DJ-CrateBuilder.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\DJ-CrateBuilder.exe"; Description: "Launch DJ-CrateBuilder"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
