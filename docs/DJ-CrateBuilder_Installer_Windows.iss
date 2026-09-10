; ============================================================================
; DJ-CrateBuilder v2.0 — Inno Setup Installer (Windows)
; Installs to Program Files with admin privileges
; ============================================================================
;
; BEFORE COMPILING:
;   1. Generate a unique GUID at https://www.guidgenerator.com/
;      and replace the AppId value below.
;   2. Update SourceDir to point to your PyInstaller dist\DJ-CrateBuilder\ folder.
;   3. (Optional) Update SetupIconFile to your .ico path.
;
; The build number is read from APP_BUILD in ..\DJ-CrateBuilder_v2.0.py at
; compile time, so the installer reports the same "2.0.<build>" the app does
; and the output file is already named for it. Override with
;   ISCC /DBUILD=73 <this file>
;
; ============================================================================

#define AppMajor "2.0"

#ifndef BUILD
  #define SourcePy AddBackslash(SourcePath) + "..\DJ-CrateBuilder_v2.0.py"
  #if !FileExists(SourcePy)
    #error DJ-CrateBuilder_v2.0.py not found next to docs\ — pass /DBUILD=N to ISCC
  #endif
  #define FH FileOpen(SourcePy)
  #define Line ""
  #define BUILD ""
  #sub ScanLine
    #expr Line = FileRead(FH)
    #if (Pos("APP_BUILD ", Line) == 1) || (Pos("APP_BUILD=", Line) == 1)
      #expr BUILD = Trim(Copy(Line, Pos("=", Line) + 1, Len(Line)))
    #endif
  #endsub
  #for {0; (BUILD == "") && !FileEof(FH); 0} ScanLine
  #expr FileClose(FH)
  #if BUILD == ""
    #error Could not read APP_BUILD from DJ-CrateBuilder_v2.0.py — pass /DBUILD=N to ISCC
  #endif
#endif

#define AppVersionFull AppMajor + "." + BUILD

[Setup]
; IMPORTANT: Replace this GUID with your own unique identifier
AppId={{738f32f9-352c-481b-8209-2b44f04502b7}
AppName=DJ-CrateBuilder
AppVersion={#AppVersionFull}
AppVerName=DJ-CrateBuilder v{#AppVersionFull}
VersionInfoVersion={#AppMajor}.{#BUILD}.0
AppPublisher=Corrupt Sintax
DefaultDirName={autopf}\DJ-CrateBuilder
DefaultGroupName=DJ-CrateBuilder
AllowNoIcons=yes
OutputDir=..\releases\Build_Output
OutputBaseFilename=DJ-CrateBuilder_v{#AppVersionFull}_Setup_Windows
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
Source: "C:\path\to\dist\DJ-CrateBuilder\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{app}"; Permissions: users-modify

[Icons]
Name: "{group}\DJ-CrateBuilder"; Filename: "{app}\DJ-CrateBuilder.exe"
Name: "{group}\Uninstall DJ-CrateBuilder"; Filename: "{uninstallexe}"
Name: "{autodesktop}\DJ-CrateBuilder"; Filename: "{app}\DJ-CrateBuilder.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\DJ-CrateBuilder.exe"; Description: "Launch DJ-CrateBuilder"; Flags: nowait postinstall skipifsilent

; Note: no PATH entry is needed. FFmpeg is bundled next to the app executable
; and the app points yt-dlp straight at it (see bundled_ffmpeg_dir() in the
; Python source), so there are no per-user (HKCU) registry changes to make.

[InstallDelete]
; A fresh installer upgrades in place over whatever build is already there
; (same AppId since v1.2), and Inno only ever ADDS files — it never removes
; what the previous build had and this one doesn't. Nightly deltas are
; additive too. Clearing the app-code folder first means every install ends
; up as exactly this build, with no stale modules from older ones. Only
; _internal: the user's data (database, links, cookies, logs) sits at the top
; of {app} and is never touched. The [Code] section below takes a safety copy
; of that data before this runs.
Type: filesandordirs; Name: "{app}\_internal"

[UninstallDelete]
; Only what the app is made of — NEVER the whole of {app}. The user's data
; lives there too: cratebuilder.db (the download history and the Watch List),
; cratebuilder_links.json, the cookie stores and both logs. Wiping "{app}"
; here is what destroyed them on the v1.3 -> v2.0 reinstall: an uninstall runs
; this section against everything in the folder, tracked or not. _internal is
; app code (nightly updates add untracked files inside it, which the normal
; uninstall would leave behind); ffmpeg.version is the FFmpeg channel's
; untracked marker. Everything else Inno already removes by name.
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\ffmpeg.version"

[Code]
{ ── Existing-install detection ─────────────────────────────────────────────
  Windows keeps one uninstall entry per AppId. Reading it tells us which
  build is being replaced and where it lives, so the Ready page can say so
  and a downgrade gets a warning instead of a silent overwrite. Installers
  before this change wrote DisplayVersion "2.0" / "1.3" with no build number;
  those compare as older than any "2.0.<n>", which is the right answer. }

var
  OldVersion: String;
  OldDir: String;
  BackupDir: String;
  BackupTaken: Boolean;

function UninstallKey(): String;
begin
  Result := ExpandConstant(
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#emit SetupSetting("AppId")}_is1');
end;

function ReadExisting(Root: Integer): Boolean;
begin
  Result := RegQueryStringValue(Root, UninstallKey(), 'DisplayVersion', OldVersion)
            and (OldVersion <> '');
  if Result then
  begin
    if not RegQueryStringValue(Root, UninstallKey(), 'InstallLocation', OldDir) then
      RegQueryStringValue(Root, UninstallKey(), 'Inno Setup: App Path', OldDir);
    OldDir := RemoveBackslashUnlessRoot(OldDir);
  end;
end;

procedure SplitVersion(V: String; var A, B, C: Integer);
var
  P: Integer;
begin
  A := 0; B := 0; C := 0;
  P := Pos('.', V);
  if P = 0 then begin A := StrToIntDef(V, 0); exit; end;
  A := StrToIntDef(Copy(V, 1, P - 1), 0);
  Delete(V, 1, P);
  P := Pos('.', V);
  if P = 0 then begin B := StrToIntDef(V, 0); exit; end;
  B := StrToIntDef(Copy(V, 1, P - 1), 0);
  Delete(V, 1, P);
  P := Pos('.', V);
  if P > 0 then V := Copy(V, 1, P - 1);
  C := StrToIntDef(V, 0);
end;

{ > 0 when Left is newer than Right, < 0 when older, 0 when equal. }
function CompareVersions(Left, Right: String): Integer;
var
  LA, LB, LC, RA, RB, RC: Integer;
begin
  SplitVersion(Left, LA, LB, LC);
  SplitVersion(Right, RA, RB, RC);
  Result := LA - RA;
  if Result = 0 then Result := LB - RB;
  if Result = 0 then Result := LC - RC;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  OldVersion := '';
  OldDir := '';
  if not ReadExisting(HKA) then
    ReadExisting(HKA32);
  Log('Existing install: version "' + OldVersion + '" in "' + OldDir + '"');
  if (OldVersion <> '') and (CompareVersions(OldVersion, '{#AppVersionFull}') > 0) then
    Result := MsgBox(
      'DJ-CrateBuilder ' + OldVersion + ' is already installed.' + #13#10#13#10 +
      'This installer is an OLDER build (' + '{#AppVersionFull}' + '). ' +
      'Your downloads, Watch List and settings are kept either way.' + #13#10#13#10 +
      'Install the older build anyway?',
      mbConfirmation, MB_YESNO) = IDYES;
end;

{ ── Safety copy of user data ───────────────────────────────────────────────
  Everything the user would miss lives at the top of the install folder
  (never inside _internal). Nothing in this
  installer deletes it, but a copy under ProgramData costs nothing and turns
  any future mistake — ours or a stray uninstaller — into a restore rather
  than a loss. One copy is kept; each install replaces the last. }

procedure CopyFileIfPresent(const SrcDir, DstDir, Name: String);
begin
  if FileExists(SrcDir + '\' + Name) then
  begin
    if CopyFile(SrcDir + '\' + Name, DstDir + '\' + Name, False) then
      BackupTaken := True
    else
      Log('Backup: could not copy ' + Name);
  end;
end;

procedure CopyTree(const Src, Dst: String);
var
  FR: TFindRec;
begin
  if not DirExists(Src) then exit;
  ForceDirectories(Dst);
  if FindFirst(Src + '\*', FR) then
  try
    repeat
      if (FR.Name <> '.') and (FR.Name <> '..') then
      begin
        if (FR.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
          CopyTree(Src + '\' + FR.Name, Dst + '\' + FR.Name)
        else if CopyFile(Src + '\' + FR.Name, Dst + '\' + FR.Name, False) then
          BackupTaken := True
        else
          Log('Backup: could not copy ' + Src + '\' + FR.Name);
      end;
    until not FindNext(FR);
  finally
    FindClose(FR);
  end;
end;

procedure BackupUserData();
var
  App: String;
begin
  BackupTaken := False;
  App := ExpandConstant('{app}');
  if not FileExists(App + '\cratebuilder.db') and
     not FileExists(App + '\cratebuilder_links.json') then
  begin
    Log('Backup: no user data in ' + App + ', nothing to copy');
    exit;
  end;
  BackupDir := ExpandConstant('{commonappdata}\DJ-CrateBuilder\install-backup');
  DelTree(BackupDir, True, True, True);
  if not ForceDirectories(BackupDir) then
  begin
    Log('Backup: could not create ' + BackupDir);
    exit;
  end;
  CopyFileIfPresent(App, BackupDir, 'cratebuilder.db');
  CopyFileIfPresent(App, BackupDir, 'cratebuilder.db-wal');
  CopyFileIfPresent(App, BackupDir, 'cratebuilder.db-shm');
  CopyFileIfPresent(App, BackupDir, 'cratebuilder_links.json');
  CopyFileIfPresent(App, BackupDir, 'cratebuilder_remote.json');
  CopyFileIfPresent(App, BackupDir, 'cookies.txt');
  CopyFileIfPresent(App, BackupDir, 'cookies.json');
  CopyFileIfPresent(App, BackupDir, 'activity.log');
  CopyTree(App + '\_cookies', BackupDir + '\_cookies');
  SaveStringToFile(BackupDir + '\README.txt',
    'Safety copy taken by the DJ-CrateBuilder ' + '{#AppVersionFull}' +
    ' installer before it replaced build "' + OldVersion + '".' + #13#10 +
    'Copy these files back into ' + App + ' (with the app closed) to restore.' + #13#10,
    False);
  Log('Backup: user data copied to ' + BackupDir);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    BackupUserData();
end;

{ ── Ready page ─────────────────────────────────────────────────────────────
  Say what is about to happen in the user's terms: which build is being
  replaced, that it is an in-place upgrade, and where the safety copy goes. }

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo,
  MemoTypeInfo, MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  S: String;
begin
  S := '';
  if OldVersion <> '' then
  begin
    S := S + 'Existing install:' + NewLine +
         Space + 'DJ-CrateBuilder ' + OldVersion;
    if OldDir <> '' then S := S + ' in ' + OldDir;
    S := S + NewLine +
         Space + 'Will be upgraded in place to ' + '{#AppVersionFull}' +
         ' (app code refreshed; your database, Watch List and cookies stay)' + NewLine +
         Space + 'Safety copy of that data: ' +
         ExpandConstant('{commonappdata}\DJ-CrateBuilder\install-backup') +
         NewLine + NewLine;
  end;
  if MemoDirInfo <> '' then S := S + MemoDirInfo + NewLine + NewLine;
  if MemoGroupInfo <> '' then S := S + MemoGroupInfo + NewLine + NewLine;
  if MemoTasksInfo <> '' then S := S + MemoTasksInfo + NewLine + NewLine;
  Result := S;
end;
