; Inno Setup 6 script for Tellinghouse AutoCut.
; Built automatically by "Build Installer.bat" (after PyInstaller), or open this file
; in Inno Setup and press Compile. Output: installer_out\AutoCut-Setup.exe
;
; Note: the ArchitecturesInstallIn64BitMode value needs Inno Setup 6.3 or
; newer; if you have an older 6.x, replace "x64compatible" with "x64".

#define MyAppName "Tellinghouse AutoCut"
#define MyAppVersion "1.2"
#define MyAppPublisher "Tellinghouse Media"
#define MyAppExeName "AutoCut.exe"

[Setup]
AppId={{B7F3D2C4-9A61-4E8F-8D25-1C64A0E3F7B9}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer_out
OutputBaseFilename=AutoCut-Setup
#if FileExists("autocut.ico")
SetupIconFile=autocut.ico
#endif
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"

[Files]
Source: "dist\AutoCut\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent
