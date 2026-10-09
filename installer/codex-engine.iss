#define MyAppName "Codex Engine"
; Version is passed in by scripts/build-installer-inno.ps1 from package.json.
#ifndef MyAppVersion
  #define MyAppVersion "0.3.11"
#endif
#define MyAppPublisher "Codex Engine"
#define MyAppExeName "Codex Engine.exe"
#define SourceDir "..\release\electron\win-unpacked"

[Setup]
AppId={{8BA77CE7-5079-4F72-90ED-7CB3BDBA9C0F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\release\installer
OutputBaseFilename=CodexEngineSetup-{#MyAppVersion}
SetupIconFile=..\assets\icons-v2\codex-engine-v2.ico
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
; Directly in the group: Windows 10/11 Start menus don't show nested subfolders.
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
; A second app in the group keeps Windows from collapsing the folder down to just
; "Codex Engine" (which hides the uninstaller), and is handy on its own.
Name: "{group}\{#MyAppName} Data Folder"; Filename: "{win}\explorer.exe"; Parameters: """{localappdata}\{#MyAppName}"""; Comment: "Your Codex Engine library, settings and logs"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// Windows' Restart Manager (Setup's "close applications automatically") can't close the
// backend and its Ollama: they have no window. So close Codex Engine ourselves, the same
// way clicking X does (the app then stops its backend and Ollama), and force-close
// whatever is still running after a grace period.

function IsRunning(const ExeName: String): Boolean;
var
  ResultCode: Integer;
begin
  // tasklist always exits 0; find /I exits 0 only when the name is in its output.
  Result := Exec(ExpandConstant('{cmd}'),
    '/C tasklist /FI "IMAGENAME eq ' + ExeName + '" /NH | find /I "' + ExeName + '" >nul',
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
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM "{#MyAppExeName}"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Waited := 0;
  while AnyAppProcessRunning() and (Waited < 15000) do
  begin
    Sleep(500);
    Waited := Waited + 500;
  end;
  // Anything left (a hung app, a backend without its window): force-close it and its children.
  if AnyAppProcessRunning() then
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM "{#MyAppExeName}"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM codex-engine-backend.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
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

