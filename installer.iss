; installer.iss -- DailyWallpaper installer
; Build: "C:\Users\win11\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer.iss

#define MyAppName "每日壁纸"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "郭欣哈"
#define MyAppExeName "DailyWallpaper.exe"

[Setup]
AppId={{8F3C9E2A-4B71-4C2D-9E55-1A2B3C4D5E6F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\DailyWallpaper
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=DailyWallpaper-Setup-{#MyAppVersion}
SetupIconFile=packaging\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinese"; MessagesFile: "packaging\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："
Name: "autostart"; Description: "开机自动启动"; GroupDescription: "启动选项："

[Files]
Source: "dist\DailyWallpaper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
var
  SeedPage: TInputOptionWizardPage;

procedure InitializeWizard;
begin
  SeedPage := CreateInputOptionPage(wpSelectDir,
    '初始审美筛选',
    '是否使用作者的默认偏好作为起点？',
    '这份筛选来自作者自己的使用记录：偏好暗色、神秘、超现实、抽象、极简的画面；' +
    '回避明亮拥挤、普通风景和常见游戏/动漫场景。' +
    '选择“不使用”则完全从你自己的评分学习，前几天推荐会比较随机。',
    True, False);
  SeedPage.Add('使用这份初始筛选（推荐）');
  SeedPage.Add('不使用，从零开始');
  SeedPage.Values[0] := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  DirName, Json: String;
begin
  if CurStep = ssPostInstall then
  begin
    DirName := ExpandConstant('{localappdata}\DailyWallpaper');
    ForceDirectories(DirName);
    if SeedPage.Values[0] then
      Json := '{"use_seed": true'
    else
      Json := '{"use_seed": false';
    if WizardIsTaskSelected('autostart') then
      Json := Json + ', "autostart": true}'
    else
      Json := Json + ', "autostart": false}';
    SaveStringToFile(DirName + '\install_prefs.json', Json, False);
  end;
end;
