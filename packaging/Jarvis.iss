#define AppName "JARVIS AI Assistant"
#define AppVersion "1.0.0"

[Setup]
AppId={{9D0B8894-71E8-43F4-BADF-13E991C66E7A}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=JARVIS
DefaultDirName={localappdata}\Programs\JARVIS
DefaultGroupName=JARVIS
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\JARVIS.exe
SetupIconFile=..\assets\jarvis.ico
OutputDir=..\release\installer
OutputBaseFilename=JARVIS-Setup
Compression=lzma2/fast
SolidCompression=yes
DiskSpanning=yes
DiskSliceSize=2100000000
SetupLogging=yes

[Files]
Source: "..\release\JARVIS-Payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autodesktop}\JARVIS"; Filename: "{app}\JARVIS.exe"; WorkingDir: "{app}"; IconFilename: "{app}\_internal\assets\jarvis.ico"
Name: "{autoprograms}\JARVIS"; Filename: "{app}\JARVIS.exe"; WorkingDir: "{app}"; IconFilename: "{app}\_internal\assets\jarvis.ico"

[Run]
Filename: "{app}\JARVIS.exe"; WorkingDir: "{app}"; Description: "JARVIS 실행"; Flags: nowait postinstall skipifsilent
