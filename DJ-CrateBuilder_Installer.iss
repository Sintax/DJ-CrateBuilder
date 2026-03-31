; ============================================================================
; DJ-CrateBuilder v1.2 — Inno Setup Installer Script
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
AppId={{PUT-YOUR-GENERATED-GUID-HERE}
AppName=DJ-CrateBuilder
AppVersion=1.2
AppVerName=DJ-CrateBuilder v1.2
AppPublisher=DJ-CrateBuilder
DefaultDirName={autopf}\DJ-CrateBuilder
DefaultGroupName=DJ-CrateBuilder
AllowNoIcons=yes
OutputDir=Output
OutputBaseFilename=DJ-CrateBuilder_v1.2_Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
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

; FFmpeg binaries (should already be in the dist folder per Packaging Guide)
; If they are in a separate location, uncomment and update these lines:
; Source: "C:\path\to\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
; Source: "C:\path\to\ffprobe.exe"; DestDir: "{app}"; Flags: ignoreversion

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
// Check if the install path is already on the user's PATH
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
    // Look for the path with and without trailing backslash
    Result := (Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0) and
              (Pos(';' + Uppercase(Param) + '\;', ';' + Uppercase(OrigPath) + ';') = 0);
end;

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
