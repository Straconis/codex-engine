#define MyAppName "Codex Engine"
; The version comes from package.json: scripts/build-installer-inno.ps1 passes it in.
#ifndef MyAppVersion
  #error Build the installer with "npm run installer:inno" (it passes /DMyAppVersion from package.json).
#endif
#define MyAppPublisher "Straconis"
#define MyAppURL "https://github.com/Straconis/codex-engine"
; Must match package.json build.appId (the app sets it too), so pinned shortcuts and the
; running app share one taskbar button.
#define MyAppUserModelID "com.codexengine.app"
#define MyAppExeName "Codex Engine.exe"
#define SourceDir "..\release\electron\win-unpacked"

[Setup]
AppId={{8BA77CE7-5079-4F72-90ED-7CB3BDBA9C0F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
VersionInfoVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\release\installer
OutputBaseFilename=CodexEngineSetup-{#MyAppVersion}
SetupIconFile=..\assets\icons-v2\codex-engine-v2.ico
Compression=lzma2/max
SolidCompression=yes
; Program Files needs admin rights; Electron 44 needs Windows 10 or later.
PrivilegesRequired=admin
MinVersion=10.0
WizardStyle=modern
; The app is built for 64-bit x86 only (ARM64 Windows runs it under emulation).
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[InstallDelete]
; Files from the previous version that the new one no longer has would otherwise stay.
; The app's own files all live in these two folders; the library and settings don't.
; Only when Codex Engine is already installed there, never in a folder that merely has them.
Type: filesandordirs; Name: "{app}\resources"; Check: IsUpgrade
Type: filesandordirs; Name: "{app}\locales"; Check: IsUpgrade

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelID}"
; Directly in the group: Windows 10/11 Start menus don't show nested subfolders.
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
; A second app in the group keeps Windows from collapsing the folder down to just
; "Codex Engine" (which hides the uninstaller), and is handy on its own.
; The app opens the folder of whoever clicks it (an admin installing for someone else
; would otherwise point everyone at the admin's own folder).
Name: "{group}\{#MyAppName} Data Folder"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--open-data-folder"; IconFilename: "{sys}\shell32.dll"; IconIndex: 3; Comment: "Your Codex Engine library, settings and logs"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelID}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// Windows' Restart Manager (Setup's "close applications automatically") can't close the
// backend and its Ollama: they have no window. So close Codex Engine ourselves, the same
// way clicking X does (the app then stops its backend and Ollama), and force-close
// whatever is still running after a grace period. Only the current user's copy: other
// people signed in to this PC keep theirs (and Setup asks them to close it if needed).

function IsUpgrade(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\{#MyAppExeName}'));
end;

function UserFilter(): String;
begin
  Result := '/FI "USERNAME eq ' + GetUserNameString() + '"';
end;

function IsRunning(const ExeName: String): Boolean;
var
  ResultCode: Integer;
begin
  // tasklist always exits 0; find /I exits 0 only when the name is in its output.
  Result := Exec(ExpandConstant('{cmd}'),
    '/C tasklist /FI "IMAGENAME eq ' + ExeName + '" ' + UserFilter() + ' /NH | find /I "' + ExeName + '" >nul',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function AnyAppProcessRunning(): Boolean;
begin
  Result := IsRunning('{#MyAppExeName}') or IsRunning('codex-engine-backend.exe');
end;

procedure CloseCodexEngine();
var
  ResultCode, Waited: Integer;
begin
  if not AnyAppProcessRunning() then
    Exit;
  // Polite close: sends the window a close request, like clicking X.
  Exec(ExpandConstant('{sys}\taskkill.exe'), UserFilter() + ' /IM "{#MyAppExeName}"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Waited := 0;
  while AnyAppProcessRunning() and (Waited < 15000) do
  begin
    Sleep(500);
    Waited := Waited + 500;
  end;
  // Anything left (a hung app, a backend without its window): force-close it and its children.
  if AnyAppProcessRunning() then
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T ' + UserFilter() + ' /IM "{#MyAppExeName}"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T ' + UserFilter() + ' /IM codex-engine-backend.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(1000);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CloseCodexEngine();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  CloseCodexEngine();
  Result := True;
end;

