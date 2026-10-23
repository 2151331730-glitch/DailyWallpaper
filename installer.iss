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
Name: "darktaskbar"; Description: "将任务栏和搜索框改成深色（适配深色壁纸）"; GroupDescription: "外观选项："

[Files]
Source: "dist\DailyWallpaper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "docs\STEAM_SETUP.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WM_SETTINGCHANGE = $001A;
  SMTO_ABORTIFHUNG = $0002;

function SendMessageTimeoutW(hWnd: HWND; Msg: UINT; wParam: Longint;
  lParam: Longint; fuFlags: UINT; uTimeout: UINT;
  var lpdwResult: DWORD): Longint;
  external 'SendMessageTimeoutW@user32.dll stdcall';

procedure ApplyDarkTaskbar();
var
  ignored: DWORD;
begin
  RegWriteDWordValue(HKCU,
    'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize',
    'SystemUsesLightTheme', 0);
  { The Search key is owned by SYSTEM on some machines; a refusal is fine. }
  RegWriteDWordValue(HKCU,
    'Software\Microsoft\Windows\CurrentVersion\Search',
    'SearchboxTaskbarMode', 1);
  { Broadcast a settings change so the shell re-reads the theme. }
  SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, 0,
    SMTO_ABORTIFHUNG, 1000, ignored);
end;

var
  SeedPage: TInputOptionWizardPage;

procedure InitializeWizard;
var
  Existing: String;
  Raw: AnsiString;
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
  { An upgrade should show the choice made last time, not quietly reset it. }
  Existing := '';
  if LoadStringFromFile(
       ExpandConstant('{localappdata}\DailyWallpaper\install_prefs.json'),
       Raw) then
    Existing := Raw;
  if Pos('"use_seed": false', Existing) > 0 then
    SeedPage.Values[0] := False;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DirName: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { The Run value is written by the app at first launch, not declared in
      the Registry section, so Setup must remove it explicitly -- otherwise
      Windows keeps trying to start an executable that no longer exists. }
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
      'DailyWallpaper');
    DirName := ExpandConstant('{localappdata}\DailyWallpaper');
    if DirExists(DirName) and not WizardSilent then
    begin
      if MsgBox('是否同时删除配置、壁纸缓存和评分数据？' + #13#10 + DirName,
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DirName, True, True, True);
    end;
  end;
end;

procedure WriteInstallPrefs(DirName: String);
var
  Path, Fresh, Existing: String;
  Raw: AnsiString;
begin
  Path := DirName + '\install_prefs.json';
  if SeedPage.Values[0] then
    Fresh := '{"use_seed": true'
  else
    Fresh := '{"use_seed": false';
  if WizardIsTaskSelected('autostart') then
    Fresh := Fresh + ', "autostart": true}'
  else
    Fresh := Fresh + ', "autostart": false}';

  Existing := '';
  if FileExists(Path) then
    if LoadStringFromFile(Path, Raw) then
      Existing := Raw;

  if Existing <> '' then
  begin
    { Keep keys this page does not own (first_run_done) and apply the
      choices made in this run. A silent upgrade has no page to read a
      choice from, so it leaves the recorded choices untouched instead of
      resetting them to the defaults. }
    if not WizardSilent then
    begin
      if SeedPage.Values[0] then
        StringChangeEx(Existing, '"use_seed": false', '"use_seed": true', True)
      else
        StringChangeEx(Existing, '"use_seed": true', '"use_seed": false', True);
      if WizardIsTaskSelected('autostart') then
        StringChangeEx(Existing, '"autostart": false', '"autostart": true', True)
      else
        StringChangeEx(Existing, '"autostart": true', '"autostart": false', True);
    end;
    SaveStringToFile(Path, Existing, False);
  end
  else
    SaveStringToFile(Path, Fresh, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  DirName: String;
begin
  if CurStep = ssPostInstall then
  begin
    DirName := ExpandConstant('{localappdata}\DailyWallpaper');
    ForceDirectories(DirName);
    WriteInstallPrefs(DirName);
    if WizardIsTaskSelected('darktaskbar') then
      ApplyDarkTaskbar();
  end;
end;
