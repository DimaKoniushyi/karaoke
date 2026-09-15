#ifndef MyAppName
  #error MyAppName is required
#endif
#ifndef MyAppVersion
  #error MyAppVersion is required
#endif
#ifndef MyAppExeName
  #error MyAppExeName is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef ThemeIconsDir
  #error ThemeIconsDir is required
#endif

[Setup]
AppId={{E734496E-2622-5565-89D3-45451D9DE7EE}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppName}
DefaultDirName={code:GetDefaultDir}
DefaultGroupName={#MyAppName}
DisableDirPage=no
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename={#MyAppName} Setup {#MyAppVersion}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
WizardStyle=modern
; Shown once before the wizard starts; Inno then localizes every built-in
; wizard string (Next/Back/Cancel, page titles, etc.) automatically for the
; rest of the run. The custom language combo on the preferences page only
; ever wrote its choice to the installed app's settings.json -- it never
; touched the installer's own UI language, so switching it there had no
; visible effect until after installation.
; On a fresh install the custom preferences page still lets the user choose
; the application language.  On an update Inno reuses the previously selected
; installer language and does not stop at a redundant language dialog.
ShowLanguageDialog=auto
UsePreviousLanguage=yes
SetupIconFile={#SetupIcon}
Compression=none
SolidCompression=no
DiskSpanning=no
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\data\electron-profile\selected-theme.ico

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительные ярлыки:"; Check: IsFreshInstall

[Files]
Source: "{#ThemeIconsDir}\dark.ico"; DestDir: "{app}\theme-icons"; Flags: ignoreversion
Source: "{#ThemeIconsDir}\light.ico"; DestDir: "{app}\theme-icons"; Flags: ignoreversion
Source: "{#ThemeIconsDir}\green.ico"; DestDir: "{app}\theme-icons"; Flags: ignoreversion
Source: "{#ThemeIconsDir}\violet.ico"; DestDir: "{app}\theme-icons"; Flags: ignoreversion
; Runtime is already compressed once. Inno only copies the archive from ISO.
Source: "{src}\app-runtime.zip"; DestDir: "{app}\.install"; Flags: external ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{code:SelectedIconPath}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{code:SelectedIconPath}"; Tasks: desktopicon; Check: IsFreshInstall

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent skipifdoesntexist; Check: EnsureApplicationExecutable

[Code]
var
  PreferencesPage: TWizardPage;
  LanguageCombo: TNewComboBox;
  ThemeCombo: TNewComboBox;
  ThemeCard: TNewStaticText;
  InstallModelsCheck: TNewCheckBox;
  RemoveUserData: Boolean;
  ModelProgressTimerID: Integer;
  ModelProgressPath: String;
  UpdateInstall: Boolean;
  ExistingLanguage: String;
  ExistingTheme: String;

function GetDefaultDir(Param: String): String;
var
  Drive: Integer;
  DrivePath: String;
begin
  Result := ExpandConstant('{userdocs}\{#MyAppName}');

  for Drive := Ord('D') to Ord('Z') do
  begin
    DrivePath := Chr(Drive) + ':\';

    if (DrivePath <> ExpandConstant('{sd}\')) and DirExists(DrivePath) then
    begin
      Result := DrivePath + '{#MyAppName}';
      Exit;
    end;
  end;
end;

function SetTimer(hWnd, nIDEvent, uElapse, lpTimerFunc: Longword): Longword;
external 'SetTimer@user32.dll stdcall';

function KillTimer(hWnd, nIDEvent: Longword): Boolean;
external 'KillTimer@user32.dll stdcall';

function InitializeUninstall: Boolean;
begin
  RemoveUserData :=
    MsgBox(
      'Удалить также настройки, кэш, библиотеку песен и записи пользователя?',
      mbConfirmation,
      MB_YESNO
    ) = IDYES;
  Result := True;
end;

procedure DeleteApplicationFilesExceptData;
var
  FindRec: TFindRec;
  EntryPath: String;
begin
  { The runtime is extracted from app-runtime.zip, so Inno does not know every
    installed filename. Delete every top-level entry except the only
    user-owned root. This also removes files introduced by newer releases. }
  if FindFirst(AddBackslash(ExpandConstant('{app}')) + '*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') and
           (CompareText(FindRec.Name, 'data') <> 0) then
        begin
          EntryPath := AddBackslash(ExpandConstant('{app}')) + FindRec.Name;
          if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
            DelTree(EntryPath, True, True, True)
          else
            DeleteFile(EntryPath);
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if RemoveUserData then
      DelTree(ExpandConstant('{app}\data'), True, True, True);
    DeleteApplicationFilesExceptData;
  end;
end;

function SelectedTheme: String; forward;

function IsFreshInstall: Boolean;
begin
  Result := not UpdateInstall;
end;

function JsonStringValue(const Payload, Key: String): String;
var
  KeyPosition: Integer;
  ValueStart: Integer;
  ValueEnd: Integer;
  Tail: String;
begin
  Result := '';
  KeyPosition := Pos('"' + Key + '"', Payload);
  if KeyPosition = 0 then
    Exit;
  Tail := Copy(Payload, KeyPosition + Length(Key) + 2, MaxInt);
  ValueStart := Pos(':', Tail);
  if ValueStart = 0 then
    Exit;
  Tail := Copy(Tail, ValueStart + 1, MaxInt);
  ValueStart := Pos('"', Tail);
  if ValueStart = 0 then
    Exit;
  Tail := Copy(Tail, ValueStart + 1, MaxInt);
  ValueEnd := Pos('"', Tail);
  if ValueEnd = 0 then
    Exit;
  Result := Copy(Tail, 1, ValueEnd - 1);
end;

procedure ReadExistingPreferences(const InstallDir: String);
var
  Payload: AnsiString;
  ThemePath: String;
  PreferencesPath: String;
  SettingsPath: String;
begin
  ExistingTheme := '';
  ExistingLanguage := '';
  ThemePath := AddBackslash(InstallDir) +
    'data\electron-profile\selected-theme.txt';
  if LoadStringFromFile(ThemePath, Payload) then
    ExistingTheme := Trim(String(Payload));

  PreferencesPath := AddBackslash(InstallDir) +
    'data\install-preferences.json';
  if LoadStringFromFile(PreferencesPath, Payload) then
  begin
    if ExistingTheme = '' then
      ExistingTheme := JsonStringValue(String(Payload), 'theme');
    ExistingLanguage := JsonStringValue(String(Payload), 'language');
  end;

  SettingsPath := AddBackslash(InstallDir) +
    'data\backend\settings.json';
  if LoadStringFromFile(SettingsPath, Payload) then
  begin
    if ExistingTheme = '' then
      ExistingTheme := JsonStringValue(String(Payload), 'theme');
    // Backend settings reflect changes made inside the installed application,
    // while install-preferences.json only contains the original setup choice.
    if JsonStringValue(String(Payload), 'language') <> '' then
      ExistingLanguage := JsonStringValue(String(Payload), 'language');
  end;
end;

procedure ApplyExistingPreferences;
begin
  if ExistingLanguage = 'ru' then
    LanguageCombo.ItemIndex := 1
  else if ExistingLanguage = 'en' then
    LanguageCombo.ItemIndex := 2
  else if ExistingLanguage = 'uk' then
    LanguageCombo.ItemIndex := 0;

  if ExistingTheme = 'light' then
    ThemeCombo.ItemIndex := 1
  else if ExistingTheme = 'green' then
    ThemeCombo.ItemIndex := 2
  else if ExistingTheme = 'violet' then
    ThemeCombo.ItemIndex := 3
  else if ExistingTheme = 'dark' then
    ThemeCombo.ItemIndex := 0;
end;

function ActiveLanguageComboIndex: Integer;
begin
  // LanguageCombo lists Ukrainian/Russian/English (0/1/2) -- a different
  // order than [Languages] -- so the language picked in Inno's own dialog
  // (ActiveLanguage) needs mapping onto the matching combo entry, otherwise
  // the app-language default would silently contradict the installer's own
  // just-chosen UI language.
  case ActiveLanguage of
    'russian': Result := 1;
    'english': Result := 2;
  else
    Result := 0; // ukrainian
  end;
end;

procedure ThemeChanged(Sender: TObject);
begin
  ThemeCard.Caption := 'A&D Voice  ·  ' + ThemeCombo.Text;
  case ThemeCombo.ItemIndex of
    1:
      begin
        ThemeCard.Color := $00F4F1EF;
        ThemeCard.Font.Color := $0031211B;
      end;
    2:
      begin
        ThemeCard.Color := $00234317;
        ThemeCard.Font.Color := $00E9FFE1;
      end;
    3:
      begin
        ThemeCard.Color := $00421F35;
        ThemeCard.Font.Color := $00F8E8FF;
      end;
  else
    begin
      ThemeCard.Color := $00170D14;
      ThemeCard.Font.Color := $00F4EAF1;
    end;
  end;
end;

procedure InitializeWizard;
var
  LanguageLabel: TNewStaticText;
  ThemeLabel: TNewStaticText;
  ModelsHint: TNewStaticText;
  InstallDir: String;
begin
  // {app} is not initialized while InitializeWizard is running. Inno has
  // already resolved DefaultDirName/UsePreviousAppDir into WizardDirValue,
  // so use that concrete path for the early update check and preference read.
  InstallDir := WizardDirValue;
  UpdateInstall :=
    FileExists(AddBackslash(InstallDir) + 'unins000.exe') or
    FileExists(AddBackslash(InstallDir) + '{#MyAppExeName}') or
    DirExists(AddBackslash(InstallDir) + 'data');
  if UpdateInstall then
    ReadExistingPreferences(InstallDir);

  PreferencesPage := CreateCustomPage(
    wpSelectDir,
    'Язык и тема A&D Voice',
    'Выберите язык программы и начальную тему. Позже их можно изменить в настройках.'
  );

  LanguageLabel := TNewStaticText.Create(PreferencesPage);
  LanguageLabel.Parent := PreferencesPage.Surface;
  LanguageLabel.Caption := 'Язык программы';
  LanguageLabel.SetBounds(0, 12, ScaleX(220), ScaleY(20));
  LanguageCombo := TNewComboBox.Create(PreferencesPage);
  LanguageCombo.Parent := PreferencesPage.Surface;
  LanguageCombo.Style := csDropDownList;
  LanguageCombo.SetBounds(0, 36, ScaleX(260), ScaleY(24));
  LanguageCombo.Items.Add('Українська');
  LanguageCombo.Items.Add('Русский');
  LanguageCombo.Items.Add('English');
  LanguageCombo.ItemIndex := ActiveLanguageComboIndex;

  ThemeLabel := TNewStaticText.Create(PreferencesPage);
  ThemeLabel.Parent := PreferencesPage.Surface;
  ThemeLabel.Caption := 'Тема оформления';
  ThemeLabel.SetBounds(0, 84, ScaleX(220), ScaleY(20));
  ThemeCombo := TNewComboBox.Create(PreferencesPage);
  ThemeCombo.Parent := PreferencesPage.Surface;
  ThemeCombo.Style := csDropDownList;
  ThemeCombo.SetBounds(0, 108, ScaleX(260), ScaleY(24));
  ThemeCombo.Items.Add('Тёмная');
  ThemeCombo.Items.Add('Светлая');
  ThemeCombo.Items.Add('Зелёная');
  ThemeCombo.Items.Add('Фиолетовая');
  ThemeCombo.ItemIndex := 0;
  ThemeCombo.OnChange := @ThemeChanged;

  if UpdateInstall then
    ApplyExistingPreferences;

  ThemeCard := TNewStaticText.Create(PreferencesPage);
  ThemeCard.Parent := PreferencesPage.Surface;
  ThemeCard.AutoSize := False;
  ThemeCard.Font.Style := [fsBold];
  ThemeCard.SetBounds(ScaleX(290), ScaleY(104), ScaleX(170), ScaleY(30));
  ThemeChanged(nil);

  InstallModelsCheck := TNewCheckBox.Create(PreferencesPage);
  InstallModelsCheck.Parent := PreferencesPage.Surface;
  InstallModelsCheck.Caption := 'Загрузить AI-модели во время установки';
  InstallModelsCheck.Checked := True;
  InstallModelsCheck.SetBounds(0, ScaleY(158), ScaleX(420), ScaleY(24));

  ModelsHint := TNewStaticText.Create(PreferencesPage);
  ModelsHint.Parent := PreferencesPage.Surface;
  ModelsHint.AutoSize := False;
  ModelsHint.WordWrap := True;
  ModelsHint.Font.Color := clGray;
  ModelsHint.Caption :=
    'Для загрузки нужен интернет, потребуется несколько гигабайт. ' +
    'Если отключить этот пункт, программа установится без моделей — ' +
    'их можно будет скачать позже в настройках A&D Voice.';
  ModelsHint.SetBounds(ScaleX(20), ScaleY(184), ScaleX(440), ScaleY(52));

  if UpdateInstall then
    InstallModelsCheck.Checked := False;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := UpdateInstall and
    ((PageID = wpSelectDir) or
     (PageID = wpSelectTasks) or
     (PageID = wpReady) or
     ((PreferencesPage <> nil) and (PageID = PreferencesPage.ID)));
end;

function SelectedTheme: String;
begin
  if UpdateInstall and (ExistingTheme <> '') then
  begin
    Result := ExistingTheme;
    Exit;
  end;
  if ThemeCombo = nil then
  begin
    Result := 'dark';
    Exit;
  end;
  case ThemeCombo.ItemIndex of
    1: Result := 'light';
    2: Result := 'green';
    3: Result := 'violet';
  else
    Result := 'dark';
  end;
end;

function SelectedLanguage: String;
begin
  if UpdateInstall and (ExistingLanguage <> '') then
  begin
    Result := ExistingLanguage;
    Exit;
  end;
  case LanguageCombo.ItemIndex of
    1: Result := 'ru';
    2: Result := 'en';
  else
    Result := 'uk';
  end;
end;

function SelectedIconPath(Param: String): String;
begin
  Result := ExpandConstant('{app}\theme-icons\') + SelectedTheme + '.ico';
end;

procedure WriteInitialPreferences;
var
  ProfileDir: String;
  SettingsDir: String;
  SettingsPath: String;
  InstallPreferencesPath: String;
  ThemePath: String;
  ThemeIconPath: String;
  Payload: String;
begin
  ProfileDir := ExpandConstant('{app}\data\electron-profile');
  SettingsDir := ExpandConstant('{app}\data\backend');
  SettingsPath := SettingsDir + '\settings.json';
  ForceDirectories(SettingsDir);
  Payload := '{' + #13#10 +
    '  "language": "' + SelectedLanguage + '",' + #13#10 +
    '  "theme": "' + SelectedTheme + '",' + #13#10 +
    '  "compute_mode": "auto"' + #13#10 +
    '}' + #13#10;
  if (not FileExists(SettingsPath)) and
     (not SaveStringToFile(SettingsPath, Payload, False)) then
    RaiseException('Не удалось сохранить начальные настройки программы.');

  InstallPreferencesPath := ExpandConstant('{app}\data\install-preferences.json');
  Payload := '{' + #13#10 +
    '  "language": "' + SelectedLanguage + '",' + #13#10 +
    '  "theme": "' + SelectedTheme + '"' + #13#10 +
    '}' + #13#10;
  if not SaveStringToFile(InstallPreferencesPath, Payload, False) then
    RaiseException('Не удалось передать выбранную тему программе.');

  ForceDirectories(ProfileDir);
  ThemePath := ProfileDir + '\selected-theme.txt';
  if not SaveStringToFile(ThemePath, SelectedTheme, False) then
    RaiseException('Не удалось сохранить иконку выбранной темы.');
  ThemeIconPath := ProfileDir + '\selected-theme.ico';
  if not CopyFile(SelectedIconPath(''), ThemeIconPath, False) then
    RaiseException('Не удалось подготовить системную иконку выбранной темы.');
end;

procedure ShowPendingInstallStep(const Status: String);
begin
  WizardForm.StatusLabel.Caption := Status;
  WizardForm.ProgressGauge.Position := 0;
  WizardForm.ProgressGauge.Style := npbstMarquee;
end;

procedure CompleteInstallProgress;
begin
  WizardForm.ProgressGauge.Style := npbstNormal;
  WizardForm.ProgressGauge.Position := WizardForm.ProgressGauge.Max;
end;

function ProgressValue(const Key: String): String;
var
  Lines: TArrayOfString;
  I: Integer;
  Prefix: String;
begin
  Result := '';
  if (ModelProgressPath = '') or
     (not LoadStringsFromFile(ModelProgressPath, Lines)) then
    Exit;
  Prefix := Key + '=';
  for I := 0 to GetArrayLength(Lines) - 1 do
    if Pos(Prefix, Lines[I]) = 1 then
    begin
      Result := Copy(Lines[I], Length(Prefix) + 1, MaxInt);
      Exit;
    end;
end;

function ApproximateTime(RemainingSeconds: Integer): String;
var
  Minutes: Integer;
begin
  Minutes := (RemainingSeconds + 59) div 60;
  if Minutes < 1 then
    Result := 'меньше минуты'
  else if Minutes < 60 then
    Result := IntToStr(Minutes) + ' мин'
  else
    Result := IntToStr(Minutes div 60) + ' ч ' + IntToStr(Minutes mod 60) + ' мин';
end;

function FormatGigabytes(Megabytes: Integer): String;
begin
  Result := IntToStr(Megabytes div 1024) + '.' +
    IntToStr(((Megabytes mod 1024) * 10) div 1024);
end;

procedure UpdateModelProgress(Sender: TObject);
var
  DownloadedMB: Integer;
  TotalMB: Integer;
  RemainingSeconds: Integer;
  ActiveModel: String;
  Status: String;
begin
  DownloadedMB := StrToIntDef(ProgressValue('downloaded_mb'), 0);
  TotalMB := StrToIntDef(ProgressValue('total_mb'), 0);
  if TotalMB <= 0 then
    Exit;

  WizardForm.ProgressGauge.Style := npbstNormal;
  WizardForm.ProgressGauge.Max := 1000;
  WizardForm.ProgressGauge.Position := DownloadedMB * 1000 div TotalMB;
  Status := 'Этап 3 из 3: загружено ' +
    FormatGigabytes(DownloadedMB) + ' из ' +
    FormatGigabytes(TotalMB) + ' ГБ';

  RemainingSeconds := StrToIntDef(ProgressValue('remaining_seconds'), -1);
  if RemainingSeconds >= 0 then
    Status := Status + ' · осталось примерно ' + ApproximateTime(RemainingSeconds);
  ActiveModel := ProgressValue('active');
  if ActiveModel <> '' then
    Status := Status + ' · ' + ActiveModel;
  WizardForm.StatusLabel.Caption := Status;
end;

procedure ModelProgressTimerProc(Arg1, Arg2, Arg3, Arg4: Longword);
begin
  UpdateModelProgress(nil);
end;

procedure StopModelProgressTimer;
begin
  if ModelProgressTimerID <> 0 then
  begin
    KillTimer(0, ModelProgressTimerID);
    ModelProgressTimerID := 0;
  end;
end;

procedure DeinitializeSetup;
begin
  StopModelProgressTimer;
end;

function EnsureApplicationExecutable: Boolean;
var
  ResultCode: Integer;
  TarExe: String;
  ArchivePath: String;
  AppExePath: String;
begin
  AppExePath := ExpandConstant('{app}\{#MyAppExeName}');
  if FileExists(AppExePath) then
  begin
    Result := True;
    Exit;
  end;

  TarExe := ExpandConstant('{sys}\tar.exe');
  ArchivePath := ExpandConstant('{app}\.install\app-runtime.zip');
  if FileExists(TarExe) and FileExists(ArchivePath) then
  begin
    if Exec(
      TarExe,
      '-xf "' + ArchivePath + '" -C "' + ExpandConstant('{app}') + '"',
      '',
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    ) and (ResultCode = 0) and FileExists(AppExePath) then
    begin
      Result := True;
      Exit;
    end;
  end;

  MsgBox(
    'A&D Voice установлен, но файл приложения отсутствует:' + #13#10 +
      AppExePath + #13#10#13#10 +
      'Установщик попытался восстановить программу из локального архива установки, но файл всё ещё недоступен.',
    mbError,
    MB_OK
  );
  Result := False;
end;

procedure InstallVirtualAudioDriver;
var
  ResultCode: Integer;
  PnpUtil: String;
  DriverInf: String;
begin
  PnpUtil := ExpandConstant('{sys}\pnputil.exe');
  DriverInf := ExpandConstant(
    '{app}\resources\backend\drivers\ADVoiceVirtualAudio\ADVoiceVirtualAudio.inf'
  );
  if not FileExists(DriverInf) then
    RaiseException('Пакет виртуального микрофона A&D Voice отсутствует.');
  WizardForm.StatusLabel.Caption := 'Установка виртуального микрофона A&D Voice...';
  if not Exec(
    PnpUtil,
    '/add-driver "' + DriverInf + '" /install',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
    RaiseException('Не удалось запустить установку виртуального микрофона A&D Voice.');
  if ResultCode <> 0 then
    RaiseException(
      'Windows отклонила драйвер виртуального микрофона A&D Voice. Код: ' +
      IntToStr(ResultCode)
    );
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  TarExe: String;
  ArchivePath: String;
  BackendExe: String;
  ModelsDir: String;
  ModelCacheDir: String;
  ModelLogPath: String;
begin
  if CurStep = ssPostInstall then
  begin
    if not UpdateInstall then
      WriteInitialPreferences;
    TarExe := ExpandConstant('{sys}\tar.exe');
    ArchivePath := ExpandConstant('{app}\.install\app-runtime.zip');

    if UpdateInstall then
      ShowPendingInstallStep(
        'Обновление A&D Voice: заменяем файлы программы, сохраняя настройки и данные...'
      )
    else if InstallModelsCheck.Checked then
      ShowPendingInstallStep(
        'Этап 2 из 3: распаковка программы. Установка ещё не завершена...'
      )
    else
      ShowPendingInstallStep(
        'Этап 2 из 2: распаковка программы. Установка ещё не завершена...'
      );

    if not FileExists(TarExe) then
      RaiseException('Windows tar.exe was not found. Windows 10/11 is required.');

    if not Exec(
      TarExe,
      '-xf "' + ArchivePath + '" -C "' + ExpandConstant('{app}') + '"',
      '',
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    ) then
      RaiseException('Could not start runtime extraction.');

    if ResultCode <> 0 then
      RaiseException('Runtime extraction failed. Exit code: ' + IntToStr(ResultCode));

    if not FileExists(ExpandConstant('{app}\{#MyAppExeName}')) then
      RaiseException('Runtime extraction completed, but the application executable is missing.');

    InstallVirtualAudioDriver;

    if UpdateInstall then
    begin
      CompleteInstallProgress;
      WizardForm.StatusLabel.Caption :=
        'Обновление A&D Voice завершено. Настройки, модели и библиотека сохранены.';
      Exit;
    end;

    if not InstallModelsCheck.Checked then
    begin
      CompleteInstallProgress;
      WizardForm.StatusLabel.Caption :=
        'Установка A&D Voice завершена. AI-модели можно скачать позже в настройках.';
      Exit;
    end;

    BackendExe := ExpandConstant('{app}\resources\backend\KaraokeBackend.exe');
    // Keep models, resumable download cache and logs inside the selected install folder.
    ModelsDir := ExpandConstant('{app}\data\models');
    ModelCacheDir := ExpandConstant('{app}\data\generated\temp\model-downloads');
    ModelLogPath := ExpandConstant('{app}\data\logs\model-install.log');
    ModelProgressPath := ExpandConstant('{app}\data\logs\model-progress.txt');
    DeleteFile(ModelProgressPath);
    ForceDirectories(ModelsDir);
    ForceDirectories(ModelCacheDir);
    ForceDirectories(ExpandConstant('{app}\data\logs'));
    ForceDirectories(ExpandConstant('{app}\data\temp'));
    ShowPendingInstallStep(
      'Этап 3 из 3: подготовка загрузки AI-моделей...'
    );
    ModelProgressTimerID := SetTimer(0, 0, 1000, CreateCallback(@ModelProgressTimerProc));
    if not Exec(
      BackendExe,
      '--install-ai-models --models-root "' + ModelsDir +
        '" --cache-dir "' + ModelCacheDir + '" --workers 1 --retries 3' +
        ' --log-file "' + ModelLogPath + '"' +
        ' --progress-file "' + ModelProgressPath + '"',
      ExpandConstant('{app}\resources\backend'),
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    ) then
    begin
      StopModelProgressTimer;
      RaiseException('Не удалось запустить загрузку AI-моделей.');
    end;
    StopModelProgressTimer;
    UpdateModelProgress(nil);
    if ResultCode <> 0 then
      RaiseException(
        'Не удалось загрузить одну из AI-моделей. Уже загруженные файлы сохранены,' + #13#10 +
        'поэтому повторная установка продолжит загрузку, а не начнёт её заново.' + #13#10#13#10 +
        'Код: ' + IntToStr(ResultCode) + #13#10 +
        'Подробный лог: ' + ModelLogPath
      );
    CompleteInstallProgress;
    WizardForm.StatusLabel.Caption := 'Установка A&D Voice завершена.';
  end;
end;
