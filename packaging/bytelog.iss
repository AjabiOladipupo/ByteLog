; Inno Setup 6 script for ByteLog. Per-user install, no admin rights needed.
; Built by packaging\build.ps1, which passes /DAppVersion=... and /DBuildDir=dist\ByteLog.
; Manual build: ISCC.exe /DAppVersion=0.1.0 /DBuildDir=..\dist\ByteLog packaging\bytelog.iss

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef BuildDir
  #define BuildDir "..\dist\ByteLog"
#endif

[Setup]
AppId={{5E28875D-2D54-4A82-BC97-351F8F24D74E}
AppName=ByteLog
AppVersion={#AppVersion}
AppVerName=ByteLog {#AppVersion}
AppPublisher=ByteLog
DefaultDirName={localappdata}\Programs\ByteLog
DefaultGroupName=ByteLog
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=ByteLog-{#AppVersion}-setup
SetupIconFile=..\src\bytelog\assets\icon.ico
UninstallDisplayIcon={app}\ByteLog.exe
UninstallDisplayName=ByteLog
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\ByteLog"; Filename: "{app}\ByteLog.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\ByteLog"; Filename: "{app}\ByteLog.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; Toast notifications are attributed by AppUserModelID; the collector also registers this
; on first run, but doing it here makes the first alert show the right name and icon.
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\ByteLog.App"; ValueType: string; ValueName: "DisplayName"; ValueData: "ByteLog"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\ByteLog.App"; ValueType: string; ValueName: "IconUri"; ValueData: "{app}\_internal\bytelog\assets\icon.png"
; Start-at-login fallback used when the scheduled task cannot be created
; (platform/windows.py RUN_VALUE). Nothing is written here; it is only removed on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "ByteLog Collector"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\ByteLogCollector.exe"; WorkingDir: "{app}"; Flags: nowait runasoriginaluser
Filename: "{app}\ByteLog.exe"; Parameters: "--tray"; WorkingDir: "{app}"; Flags: nowait runasoriginaluser
Filename: "{app}\ByteLog.exe"; Description: "Open the ByteLog dashboard"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent runasoriginaluser unchecked

[UninstallRun]
Filename: "{app}\ByteLogCollector.exe"; Parameters: "--stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopCollector"
Filename: "{sys}\schtasks.exe"; Parameters: "/delete /tn ""ByteLog Collector"" /f"; Flags: runhidden waituntilterminated; RunOnceId: "DeleteTask"
Filename: "{sys}\taskkill.exe"; Parameters: "/im ByteLog.exe /f"; Flags: runhidden waituntilterminated; RunOnceId: "CloseDashboard"

; Usage data in %LOCALAPPDATA%\ByteLog is intentionally kept on uninstall.

[Code]
// On upgrade, ask the running collector to exit so its files can be replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Collector: String;
  ResultCode: Integer;
begin
  Result := '';
  Collector := ExpandConstant('{app}\ByteLogCollector.exe');
  if FileExists(Collector) then
  begin
    Exec(Collector, '--stop', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(3000);
  end;
end;
