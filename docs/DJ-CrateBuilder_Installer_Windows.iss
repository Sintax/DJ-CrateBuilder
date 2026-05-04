; ============================================================================
; DJ-CrateBuilder v1.3 — Inno Setup Installer (Windows)
; Installs to Program Files with admin privileges
; ============================================================================
;
; BEFORE COMPILING:
;   1. Generate a unique GUID at https://www.guidgenerator.com/
;      and replace the AppId value below.
;   2. Update SourceDir to point to your PyInstaller dist\DJ-CrateBuilder\ folder.
;   3. (Optional) Update SetupIconFile to your .ico path.
;
; ============================================================================

[Setup]
; IMPORTANT: Replace this GUID with your own unique identifier
AppId={{738f32f9-352c-481b-8209-2b44f04502b7}
AppName=DJ-CrateBuilder
AppVersion=1.3
AppVerName=DJ-CrateBuilder v1.3
AppPublisher=Corrupt Sintax
DefaultDirName={autopf}\DJ-CrateBuilder
DefaultGroupName=DJ-CrateBuilder
AllowNoIcons=yes
OutputDir=Output
OutputBaseFilename=DJ-CrateBuilder_v1.3_Setup_Windows
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
DisableProgramGroupPage=yes
; Uncomment and set path to use a custom icon:
; SetupIconFile=C:\path\to\icon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; IMPORTANT: Update this path to your PyInstaller output folder
Source: "C:\Users\djsin\Documents\Claude.ai Projects\DJ-CrateBuilder\v_1.3\dist\DJ-CrateBuilder\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{app}"; Permissions: users-modify

[Icons]
Name: "{group}\DJ-CrateBuilder"; Filename: "{app}\DJ-CrateBuilder.exe"
Name: "{group}\Uninstall DJ-CrateBuilder"; Filename: "{uninstallexe}"
Name: "{autodesktop}\DJ-CrateBuilder"; Filename: "{app}\DJ-CrateBuilder.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\DJ-CrateBuilder.exe"; Description: "Launch DJ-CrateBuilder"; Flags: nowait postinstall skipifsilent

[Registry]
; Add install directory to user PATH so FFmpeg is accessible
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
    ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
function NeedsAddPath(Param: string): boolean;
var
    OrigPath: string;
begin
    if not RegQueryStringValue(HKEY_CURRENT_USER,
        'Environment', 'Path', OrigPath)
    then begin
        Result := True;
        exit;
    end;
    Result := (Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0) and
              (Pos(';' + Uppercase(Param) + '\;', ';' + Uppercase(OrigPath) + ';') = 0);
end;

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
