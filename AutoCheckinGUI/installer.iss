; ============================================================================
; Trae & Buddy 签到助手 —— Inno Setup 安装脚本
; ----------------------------------------------------------------------------
; 用途：把 dist\TraeBuddy签到助手\ 打包成单文件安装程序（Setup.exe）。
;
; 使用方式（二选一）：
;   A. 已安装 Inno Setup 6：右键本文件 → Compile，或在命令行执行
;        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
;   B. 未安装 Inno Setup：改用同目录下的 make_installer.py（纯 Python，
;      生成自解压安装程序 + 卸载项，无需任何外部工具）。
;
; 产物：Output\TraeBuddy签到助手_Setup_2.4.6.exe
; ============================================================================

#define AppName        "Trae & Buddy 签到助手"
#define AppShortName   "TraeBuddy"
#define AppVersion     "2.4.6"
#define AppPublisher   "极泊Poles"
#define AppURL         "https://github.com/cxqc168-wq"
#define AppExeName     "TraeBuddy签到助手.exe"
#define SourceDir      "dist\TraeBuddy签到助手"

[Setup]
AppId={{8F3A2C71-5B4D-4E19-9C2A-TRAEBUDDY246}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppShortName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename={#AppShortName}_Setup_{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
SetupIconFile=assets\app.ico
; 允许用户改安装目录（便于放到 D 盘等）
DisableDirPage=no

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: checkedonce
Name: "startupicon"; Description: "开机自动启动图形界面"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
; 整个目录版 exe 全部文件
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; 开机自启（可选任务）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "{#AppShortName}"; \
  ValueData: """{app}\{#AppExeName}"""; \
  Flags: uninsdeletevalue; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载时移除计划任务（若安装过）
Filename: "schtasks.exe"; Parameters: "/Delete /TN AutoDailyCheckin /F"; \
  Flags: runhidden; RunOnceId: "DelTask"

[UninstallDelete]
; 只删程序自身目录；用户数据（logs/config.json/profiles）保留，
; 避免误删签到记录。如需彻底清理，用户可手动删除安装目录。
Type: filesandordirs; Name: "{app}\_internal"
