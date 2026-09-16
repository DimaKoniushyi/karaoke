@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "KARAOKE_LYRICS_LOG_TEXT=1"
set "KARAOKE_BACKEND_URL=http://127.0.0.1:18000"
set "KARAOKE_RENDERER_ORIGIN=http://127.0.0.1:5173"
set "SONGAPP_API_TOKEN=advoice-local-development"
set "VITE_API_BASE_URL=http://127.0.0.1:18000"
set "VITE_API_TOKEN=advoice-local-development"
set "PREPARE_ONLY=0"
if /i "%~1"=="--prepare-only" set "PREPARE_ONLY=1"
if /i "%~1"=="--job" goto :job
title A^&D Voice - Development

set "ROOT=%~dp0"
set "BACK=%ROOT%backend"
set "FRONT=%ROOT%front"
set "VENV=%BACK%\venv"
set "PY=%VENV%\Scripts\python.exe"

set "VER=3.12.10"
set "RT=%ROOT%downloads\runtimes\python312"
set "RTPY=%RT%\tools\python.exe"
set "PKG=%TEMP%\advoice-python-%VER%.zip"
set "URL=https://api.nuget.org/v3-flatcontainer/python/%VER%/python.%VER%.nupkg"

set "AI=%ROOT%scripts\install-ai-models.bat"
set "ASIO=%ROOT%scripts\prepare-native-audio.bat"
set "JOBS=%TEMP%\advoice-dev-%RANDOM%-%RANDOM%"
set "NODE_ENV_FILE=%TEMP%\advoice-node-%RANDOM%-%RANDOM%.path"
set "FFMPEG_ENV_FILE=%TEMP%\advoice-ffmpeg-%RANDOM%-%RANDOM%.path"

set "PYTHONHOME="
set "PYTHONPATH="

for %%J in (FRONT AI ASIO) do (
    set "%%J_RC=%JOBS%\%%J.rc"
    set "%%J_LOG=%JOBS%\%%J.log"
)

echo.
echo ============================================================
echo  A^&D Voice - Development
echo ============================================================
echo.

for %%D in ("%BACK%" "%FRONT%") do if not exist "%%~D\" (
    echo [ERROR] Directory not found:
    echo   %%~D
    goto :err
)

for %%F in ("%AI%" "%ASIO%") do if not exist "%%~F" (
    echo [ERROR] Script not found:
    echo   %%~F
    goto :err
)

echo Checking the project Node.js runtime...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\ensure-node.ps1" -Root "%ROOT%." -OutputEnvironmentFile "%NODE_ENV_FILE%"
if errorlevel 1 goto :err
if not exist "%NODE_ENV_FILE%" goto :err
for /f "usebackq delims=" %%P in ("%NODE_ENV_FILE%") do set "PATH=%%P;%PATH%"
del /q "%NODE_ENV_FILE%" >nul 2>&1
where node.exe >nul 2>&1 || goto :err
where npm.cmd >nul 2>&1 || goto :err

echo Checking native build tools and FFmpeg...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\ensure-dev-tools.ps1" -Root "%ROOT%." -OutputEnvironmentFile "%FFMPEG_ENV_FILE%"
if errorlevel 1 goto :err
if not exist "%FFMPEG_ENV_FILE%" goto :err
for /f "usebackq delims=" %%P in ("%FFMPEG_ENV_FILE%") do set "PATH=%%P;%PATH%"
del /q "%FFMPEG_ENV_FILE%" >nul 2>&1
where ffmpeg.exe >nul 2>&1 || goto :err

if "%PREPARE_ONLY%"=="0" (
    call :stop_dev_processes
    if errorlevel 1 goto :err
)

mkdir "%JOBS%" >nul 2>&1 || goto :err

call :start_job front "%FRONT%" "" "%FRONT_LOG%" "%FRONT_RC%"
call :start_job asio "%ASIO%" "%ROOT%" "%ASIO_LOG%" "%ASIO_RC%"

rem ============================================================================
rem PYTHON
rem ============================================================================

if exist "%PY%" (
    "%PY%" -c "import sys;raise SystemExit(0 if sys.version_info[:3]==(3,12,10) else 1)" >nul 2>&1
    if not errorlevel 1 goto :venv
    echo Recreating invalid virtual environment...
    rmdir /s /q "%VENV%" >nul 2>&1
)

call :runtime
if errorlevel 1 goto :err

echo.
echo Creating backend virtual environment:
echo   %VENV%
echo.

"%RTPY%" -m venv "%VENV%"
if errorlevel 1 goto :err

if not exist "%PY%" (
    echo [ERROR] Virtual environment was not created:
    echo   %VENV%
    goto :err
)

"%PY%" -m pip install --disable-pip-version-check --prefer-binary -U pip setuptools wheel
if errorlevel 1 goto :err

if exist "%BACK%\requirements.txt" (
    if exist "%BACK%\requirements-dev.txt" (
        "%PY%" -m pip install --disable-pip-version-check --prefer-binary ^
            -r "%BACK%\requirements.txt" ^
            -r "%BACK%\requirements-dev.txt"
    ) else (
        "%PY%" -m pip install --disable-pip-version-check --prefer-binary ^
            -r "%BACK%\requirements.txt"
    )
    if errorlevel 1 goto :err
) else if exist "%BACK%\requirements-dev.txt" (
    "%PY%" -m pip install --disable-pip-version-check --prefer-binary ^
        -r "%BACK%\requirements-dev.txt"
    if errorlevel 1 goto :err
)

:venv
call "%VENV%\Scripts\activate.bat"
if errorlevel 1 goto :err

echo.
echo Python:
"%PY%" --version
echo Venv:
echo   %VIRTUAL_ENV%
echo.

call :start_job ai "%AI%" "%ROOT%" "%AI_LOG%" "%AI_RC%"

rem ============================================================================
rem WAIT
rem ============================================================================

echo.
echo ============================================================
echo  Preparing development environment
echo ============================================================
echo.

call :wait

for %%J in (FRONT ASIO AI) do (
    call :result %%J
    if errorlevel 1 goto :err
)

if exist "%ROOT%downloads\ai-environment.bat" (
    echo Loading AI environment...
    call "%ROOT%downloads\ai-environment.bat"
    if errorlevel 1 goto :err
)

rmdir /s /q "%JOBS%" >nul 2>&1
set "KARAOKE_PYTHON=%PY%"

if "%PREPARE_ONLY%"=="1" (
    echo.
    echo ============================================================
    echo  Development dependencies are ready
    echo ============================================================
    echo.
    exit /b 0
)

echo.
echo ============================================================
echo  Starting A^&D Voice
echo ============================================================
echo.

cd /d "%FRONT%" || goto :err
call npm run dev:electron
exit /b %errorlevel%

rem ============================================================================
rem STOP PREVIOUS DEVELOPMENT INSTANCE
rem ============================================================================

:stop_dev_processes
echo Stopping previous A^&D Voice development processes...
set "ADVOICE_DEV_ROOT=%ROOT%"

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
"$p=18000,5173;$root=[IO.Path]::GetFullPath($env:ADVOICE_DEV_ROOT).TrimEnd('\');$names=@('node.exe','electron.exe','python.exe','pythonw.exe');function Get-Targets{$ids=[Collections.Generic.HashSet[int]]::new();foreach($proc in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)){if($names -notcontains [string]$proc.Name){continue};$exe=[string]$proc.ExecutablePath;$cmd=[string]$proc.CommandLine;$owned=$exe.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)-or $cmd.IndexOf($root,[StringComparison]::OrdinalIgnoreCase)-ge 0;$editorTool=$cmd.IndexOf('\.vscode\extensions\',[StringComparison]::OrdinalIgnoreCase)-ge 0;if($owned -and -not $editorTool){[void]$ids.Add([int]$proc.ProcessId)}};foreach($x in @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)){if($p -contains [int]$x.LocalPort){[void]$ids.Add([int]$x.OwningProcess)}};return @($ids)};for($pass=0;$pass -lt 3;$pass++){$targets=@(Get-Targets);if($targets.Count -eq 0){break};foreach($target in $targets){if($target -gt 4 -and $target -ne $PID -and (Get-Process -Id $target -ErrorAction SilentlyContinue)){Start-Process -FilePath taskkill.exe -ArgumentList '/PID',([string]$target),'/T','/F' -Wait -WindowStyle Hidden}};Start-Sleep -Milliseconds 350};$left=@(Get-Targets);if($left.Count -gt 0){Write-Host ('[ERROR] Could not stop process IDs: '+($left -join ', '));exit 1}"

set "STOP_RC=%errorlevel%"
set "ADVOICE_DEV_ROOT="
if not "%STOP_RC%"=="0" exit /b %STOP_RC%

echo Previous development processes stopped.
echo.
exit /b 0

rem ============================================================================
rem START JOB
rem ============================================================================

:start_job
echo Starting %~1 preparation...
start "" /b cmd.exe /c call "%~f0" --job "%~1" "%~2" "%~3" "%~4" "%~5"
exit /b 0

rem ============================================================================
rem LOCAL PYTHON
rem ============================================================================

:runtime
if exist "%RTPY%" (
    call :check_runtime
    if not errorlevel 1 exit /b 0
    echo Existing local Python runtime is invalid. Recreating...
)

echo Preparing local Python %VER%...

rmdir /s /q "%RT%" >nul 2>&1
del /q "%PKG%" >nul 2>&1
mkdir "%RT%" || exit /b 1

echo Downloading Python %VER%...

where curl.exe >nul 2>&1
if not errorlevel 1 (
    curl.exe -LfsS --retry 5 --retry-delay 2 -o "%PKG%" "%URL%"
)

if not exist "%PKG%" (
    echo curl failed. Trying PowerShell...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference='SilentlyContinue';Invoke-WebRequest -UseBasicParsing -Uri '%URL%' -OutFile '%PKG%'"
    if errorlevel 1 exit /b 1
)

if not exist "%PKG%" (
    echo [ERROR] Python download failed.
    exit /b 1
)

for %%F in ("%PKG%") do if %%~zF==0 (
    echo [ERROR] Downloaded Python package is empty.
    exit /b 1
)

echo Extracting Python...

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
"Expand-Archive -LiteralPath '%PKG%' -DestinationPath '%RT%' -Force"

if errorlevel 1 (
    echo [ERROR] Python extraction failed.
    exit /b 1
)

del /q "%PKG%" >nul 2>&1

call :check_runtime
exit /b %errorlevel%

:check_runtime
if not exist "%RTPY%" (
    echo [ERROR] Python executable not found:
    echo   %RTPY%
    exit /b 1
)

"%RTPY%" -c "import sys;raise SystemExit(0 if sys.version_info[:3]==(3,12,10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Expected Python %VER%.
    exit /b 1
)

"%RTPY%" -c "import venv" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python venv module is unavailable.
    exit /b 1
)

exit /b 0

rem ============================================================================
rem WAIT / STATUS
rem ============================================================================

:wait
set /a SEC=0

:wait_loop
if exist "%FRONT_RC%" if exist "%ASIO_RC%" if exist "%AI_RC%" (
    echo.
    echo Parallel preparation finished.
    exit /b 0
)

set /a MOD=SEC%%15

if !MOD!==0 (
    for %%J in (FRONT AI ASIO) do call :state %%J
    echo.
    echo [STATUS !SEC!s] Frontend: !FRONT_STATE! ^| AI Core: !AI_STATE! ^| ASIO: !ASIO_STATE!
    for %%J in (FRONT AI ASIO) do call :tail %%J
)

ping.exe 127.0.0.1 -n 2 >nul
set /a SEC+=1
goto :wait_loop

:state
call set "R=%%%~1_RC%%"
set "%~1_STATE=RUNNING"

if not exist "%R%" exit /b 0

set "C="
set /p C=<"%R%"

if "!C!"=="0" (
    set "%~1_STATE=DONE"
) else (
    set "%~1_STATE=FAILED"
)

exit /b 0

:tail
setlocal DisableDelayedExpansion
call set "L=%%%~1_LOG%%"

if not exist "%L%" (
    endlocal
    exit /b 0
)

set "ADVOICE_LOG=%L%"
set "ADVOICE_NAME=%~1"

for /f "usebackq delims=" %%L in (`
    powershell.exe -NoProfile -Command ^
    "$p=$env:ADVOICE_LOG;if(Test-Path -LiteralPath $p){Get-Content -LiteralPath $p -Tail 3 -ErrorAction SilentlyContinue|Where-Object{-not [string]::IsNullOrWhiteSpace($_)}|ForEach-Object{'  '+$env:ADVOICE_NAME+': '+$_}}"
`) do echo %%L

endlocal
exit /b 0

:result
call set "R=%%%~1_RC%%"
call set "L=%%%~1_LOG%%"

if exist "%R%" (
    set "C="
    set /p C=<"%R%"
    if "!C!"=="0" (
        echo [%~1] DONE
        exit /b 0
    )
)

echo.
echo ============================================================
echo [ERROR] %~1 preparation failed.
echo ============================================================
echo.

if exist "%L%" type "%L%"
exit /b 1

rem ============================================================================
rem JOB
rem ============================================================================

:job
setlocal EnableExtensions EnableDelayedExpansion

set "NAME=%~2"
set "TARGET=%~3"
set "ARG=%~4"
set "LOG=%~5"
set "JOB_RESULT=%~6"

>"%LOG%" echo %NAME% worker started.

if /i "%NAME%"=="front" (
    cd /d "%TARGET%" >>"%LOG%" 2>&1 || goto :job_fail

    if exist "%TARGET%\node_modules\" (
        >>"%LOG%" echo Frontend dependencies already exist.
        >"%JOB_RESULT%" echo 0
        exit /b 0
    )

    if exist "%TARGET%\package-lock.json" (
        call npm ci >>"%LOG%" 2>&1
    ) else (
        call npm install >>"%LOG%" 2>&1
    )
) else (
    if not exist "%TARGET%" goto :job_fail
    call "%TARGET%" "%ARG%" >>"%LOG%" 2>&1
)

set "E=!errorlevel!"
>"%JOB_RESULT%" echo !E!
exit /b !E!

:job_fail
>"%JOB_RESULT%" echo 1
exit /b 1

rem ============================================================================
rem ERROR
rem ============================================================================

:err
if defined NODE_ENV_FILE del /q "%NODE_ENV_FILE%" >nul 2>&1
if defined FFMPEG_ENV_FILE del /q "%FFMPEG_ENV_FILE%" >nul 2>&1
echo.
echo ============================================================
echo [ERROR] Development environment could not be started.
echo ============================================================
echo.

if defined JOBS (
    echo Logs:
    echo   %JOBS%
    echo.
)

pause
exit /b 1
