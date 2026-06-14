; Inno Setup script for ClaudeUsage — per-user, no-admin installer.
;
; Built by CI (and locally) with:
;   iscc /DAppVersion=1.2.3 installer\ClaudeUsage.iss
; Input:  ..\dist\ClaudeUsage.exe   (the PyInstaller --onefile build)
; Output: ..\dist\ClaudeUsageSetup.exe
;
; Installs to %LOCALAPPDATA%\Programs\ClaudeUsage (no UAC) — the SAME directory
; the app swaps its own exe in during self-update, so updates stay unprivileged.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Stable AppId so version upgrades replace the install instead of duplicating it.
AppId={{B7C4D2E1-9A3F-4E6B-8C1D-2F5A7E9B0C3D}
AppName=ClaudeUsage
AppVersion={#AppVersion}
AppPublisher=stonelym
DefaultDirName={localappdata}\Programs\ClaudeUsage
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ClaudeUsageSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Keep the wizard minimal — it "just works".
DisableReadyPage=yes

[Tasks]
Name: "startup"; Description: "Start ClaudeUsage when I sign in"; GroupDescription: "Startup:"

[Files]
Source: "..\dist\ClaudeUsage.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\ClaudeUsage"; Filename: "{app}\ClaudeUsage.exe"

[Registry]
; Same HKCU Run value the tray "Run at startup" toggle uses, so they stay in sync.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "ClaudeUsage"; \
    ValueData: """{app}\ClaudeUsage.exe"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\ClaudeUsage.exe"; Description: "Launch ClaudeUsage"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove the install dir incl. any self-update leftovers (.old/.new exe).
Type: filesandordirs; Name: "{app}"

[Code]
// Close a running instance so its locked exe can be replaced (install/upgrade).
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im ClaudeUsage.exe', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
