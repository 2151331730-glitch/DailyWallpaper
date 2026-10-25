; installer.iss -- DailyWallpaper installer
; Build: "C:\Users\win11\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer.iss

#define MyAppName "每日壁纸"
#define MyAppVersion "1.1.0"
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

[InstallDelete]
; An upgrade copies over same-named files and leaves everything else alone, so
; a payload dropped in an older release (an old `dailywallpaper-0.1.0.dist-info`,
; a Qt plugin no longer shipped) stayed in `_internal` forever. The bundled
; metadata is what `--version` and the about box read, and with two of them the
; old one won: an upgrade from 0.1.x to 1.0.1 still reported 0.1.0. The whole
; payload is rebuilt from source on every release, so clear it before copying.
; User data lives in {localappdata}\DailyWallpaper and is untouched.
Type: filesandordirs; Name: "{app}\_internal"

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
  original: Cardinal;
begin
  { Remember the old value so uninstall can put the user's theme back.
    A missing value means Windows' default (light); nothing to save. }
  if RegQueryDWordValue(HKCU,
       'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize',
       'SystemUsesLightTheme', original) then
    RegWriteDWordValue(HKCU, 'Software\DailyWallpaper',
      'OrigSystemUsesLightTheme', original);
  { Both writes can be refused (the Search key is owned by SYSTEM on some
    machines). Inno raises on a refused RegWrite*, so each one is guarded:
    an install must never stop with an error dialog because of a theme. }
  try
    RegWriteDWordValue(HKCU,
      'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize',
      'SystemUsesLightTheme', 0);
  except
  end;
  try
    RegWriteDWordValue(HKCU,
      'Software\Microsoft\Windows\CurrentVersion\Search',
      'SearchboxTaskbarMode', 1);
  except
  end;
  { Broadcast a settings change so the shell re-reads the theme. }
  SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, 0,
    SMTO_ABORTIFHUNG, 1000, ignored);
end;

var
  SeedPage: TInputOptionWizardPage;
  WePage: TInputOptionWizardPage;

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

  { Wallpaper Engine starts through an auto-start Windows service, before
    Explorer is up, and puts its own live wallpaper back at every login: that
    is the old image a new user sees for the first half minute. Turning it off
    is the only way to make the boot show this app's wallpaper from the start.
    The choice is remembered so an upgrade does not silently undo it. }
  WePage := CreateInputOptionPage(SeedPage.ID,
    'Wallpaper Engine 的开机自启动',
    '要不要关闭 Wallpaper Engine 的开机自启动？',
    'Wallpaper Engine 会在登录时（比桌面出现还早）恢复它自己的动态壁纸，' +
    '所以开机会先看到一张旧壁纸，过一会儿才被本程序换掉。' +
    '关闭它不会影响动态壁纸：需要动态壁纸时，本程序会自己把 WE 启动起来。' +
    '关闭会结束正在运行的 Wallpaper Engine，并需要管理员权限（安装过程中弹一次 UAC）。' +
    '卸载本程序时，这些开关会自动还原。',
    True, False);
  WePage.Add('关闭 Wallpaper Engine 的开机自启动（推荐）');
  WePage.Add('保留，我不介意开机先看到旧壁纸');
  WePage.Values[0] := True;
  if Pos('"disable_we_autostart": false', Existing) > 0 then
    WePage.Values[0] := False;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DirName: String;
  ResultCode: Integer;
  ignored: DWORD;
  original: Cardinal;
begin
  if CurUninstallStep = usUninstall then
  begin
    { A tray process left running keeps the exe locked, so the uninstaller
      cannot remove it cleanly. Stop it, then let the app delete the keys it
      stored in the Windows credential manager. }
    Exec(ExpandConstant('{sys}\taskkill.exe'),
      '/F /IM DailyWallpaper.exe', '', SW_HIDE, ewWaitUntilTerminated,
      ResultCode);
    Exec(ExpandConstant('{app}\{#MyAppExeName}'), '--forget', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
    { Put back the taskbar/theme choice the installer changed. }
    if RegQueryDWordValue(HKCU, 'Software\DailyWallpaper',
         'OrigSystemUsesLightTheme', original) then
    begin
      try
        RegWriteDWordValue(HKCU,
          'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize',
          'SystemUsesLightTheme', original);
      except
      end;
      RegDeleteValue(HKCU, 'Software\DailyWallpaper',
        'OrigSystemUsesLightTheme');
      SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, 0,
        SMTO_ABORTIFHUNG, 1000, ignored);
    end;
  end;
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

function WantDisableWe(): Boolean;
begin
  { A silent install has no page to agree on this: taking another program's
    autostart away (and prompting for UAC) without being asked would be wrong,
    so only an interactive install can turn it off. }
  if WizardSilent then
    Result := False
  else
    Result := WePage.Values[0];
end;

procedure DisableWeAutostart();
var
  ResultCode: Integer;
begin
  { The app owns this logic (it also knows where WE is installed and which of
    the service / Run-key mechanisms that install uses), so Setup only calls
    it. A declined UAC prompt comes back as a non-zero exit code. }
  if not Exec(ExpandConstant('{app}\{#MyAppExeName}'), '--disable-we-autostart',
              ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated,
              ResultCode) then
  begin
    MsgBox('没能运行 {#MyAppExeName} 来关闭 Wallpaper Engine 的开机自启动。',
           mbInformation, MB_OK);
    Exit;
  end;
  if ResultCode <> 0 then
    MsgBox('没能关闭 Wallpaper Engine 的开机自启动（可能拒绝了管理员权限请求）。'
           + #13#10 +
           '可以稍后在 Wallpaper Engine 的「设置 → 常规」里手动取消勾选'
           + '「随 Windows 启动」。', mbInformation, MB_OK);
end;

function BoolWord(B: Boolean): String;
begin
  if B then
    Result := 'true'
  else
    Result := 'false';
end;

procedure EnsurePref(var Json: String; Name, Value: String);
begin
  { An install over an existing data directory meets a prefs file the app
    wrote years of choices into; it holds no key this page owns, so the
    StringChangeEx above has nothing to replace and the choice would be
    forgotten on the next upgrade. Insert the missing key instead. }
  if Pos('"' + Name + '"', Json) > 0 then
    Exit;
  if Pos('{}', Json) > 0 then
    StringChangeEx(Json, '{}', '{"' + Name + '": ' + Value + '}', False)
  else
    StringChangeEx(Json, '}', ', "' + Name + '": ' + Value + '}', False);
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
    Fresh := Fresh + ', "autostart": true'
  else
    Fresh := Fresh + ', "autostart": false';
  if WantDisableWe() then
    Fresh := Fresh + ', "disable_we_autostart": true}'
  else
    Fresh := Fresh + ', "disable_we_autostart": false}';

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
      if WantDisableWe() then
        StringChangeEx(Existing, '"disable_we_autostart": false',
                       '"disable_we_autostart": true', True)
      else
        StringChangeEx(Existing, '"disable_we_autostart": true',
                       '"disable_we_autostart": false', True);
      EnsurePref(Existing, 'use_seed', BoolWord(SeedPage.Values[0]));
      EnsurePref(Existing, 'autostart',
                 BoolWord(WizardIsTaskSelected('autostart')));
      EnsurePref(Existing, 'disable_we_autostart', BoolWord(WantDisableWe()));
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
    if WantDisableWe() then
      DisableWeAutostart();
    if WizardIsTaskSelected('darktaskbar') then
      ApplyDarkTaskbar();
  end;
end;
