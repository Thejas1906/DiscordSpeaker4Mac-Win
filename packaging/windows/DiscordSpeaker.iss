; Inno Setup 6 script. Build with: iscc /DAppVersion=2.0.0 packaging\windows\DiscordSpeaker.iss
#ifndef AppVersion
  #define AppVersion "2.0.0"
#endif
[Setup]
AppId={{6C1D2F0E-5B7A-4E3D-9E45-DISCORDSPEAKER}
AppName=DiscordSpeaker
AppVersion={#AppVersion}
AppPublisher=DiscordSpeaker
DefaultDirName={autopf}\DiscordSpeaker
DefaultGroupName=DiscordSpeaker
UninstallDisplayIcon={app}\DiscordSpeaker.exe
OutputDir=..\..\dist
OutputBaseFilename=DiscordSpeaker-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start DiscordSpeaker when I sign in (minimised to the tray)"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\..\dist\DiscordSpeaker\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\DiscordSpeaker"; Filename: "{app}\DiscordSpeaker.exe"
Name: "{autodesktop}\DiscordSpeaker"; Filename: "{app}\DiscordSpeaker.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "DiscordSpeaker"; ValueData: """{app}\DiscordSpeaker.exe"" --minimized"; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\DiscordSpeaker.exe"; Description: "Launch DiscordSpeaker"; Flags: nowait postinstall skipifsilent
