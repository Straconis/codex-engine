#define MyAppName "Codex Engine"
; Version is passed in by scripts/build-installer-inno.ps1 from package.json.
#ifndef MyAppVersion
  #define MyAppVersion "0.3.4"
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
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

