@echo off
echo ========================================
echo  Claude Code Voice Mode Launcher
echo  AllTalk TTS  :  port 7851
echo  Whisper STT  :  port 8787
echo ========================================
echo.
echo  Choose launch mode:
echo    1. Terminal (manual CLI)
echo    2. VS Code (workspace)
echo.
choice /C 12 /N /M "Enter choice (1 or 2): "
if errorlevel 2 goto vscode_mode
if errorlevel 1 goto terminal_mode

:terminal_mode
set LAUNCH_MODE=terminal
goto start_services

:vscode_mode
set LAUNCH_MODE=vscode
goto start_services

:start_services

REM --- Detect already-running mic panel ---
set MIC_RUNNING=0
tasklist /fi "WINDOWTITLE eq Claude Code Voice Mode Mic*" 2>nul | find /i "python" >nul 2>&1
if not errorlevel 1 set MIC_RUNNING=1

REM --- Start Microphone Control Panel (it auto-starts Whisper + AllTalk) ---
if "%MIC_RUNNING%"=="1" (
    echo [1/1] Microphone Control Panel already running - skipping
) else (
    echo [1/1] Starting Microphone Control Panel...
    echo        (Mic Panel will auto-start Whisper and AllTalk)
    start "" /d "F:\Apps\freedom_system\REPO_claude_code_voice_mode" venv\Scripts\pythonw.exe mic_panel.py
)

REM --- Wait for services (now started by Mic Panel) ---
echo.
echo Waiting for services to be ready...
:wait_loop
timeout /t 3 /nobreak >nul 2>&1

REM Check AllTalk
curl -s http://127.0.0.1:7851/api/ready >nul 2>&1
if errorlevel 1 (
    echo   Waiting for AllTalk TTS...
    goto wait_loop
)

REM Check Whisper
curl -s http://127.0.0.1:8787/health >nul 2>&1
if errorlevel 1 (
    echo   Waiting for Whisper STT...
    goto wait_loop
)

echo.
echo ========================================
echo  All services ready!
echo  AllTalk TTS:  http://127.0.0.1:7851
echo  Whisper STT:  http://127.0.0.1:8787
echo  Mic Panel:    Running (controls all services)
echo ========================================
echo.

if "%LAUNCH_MODE%"=="vscode" goto launch_vscode
goto launch_terminal

:launch_vscode
echo Starting VS Code with Claude Code...
"F:\Apps\VSCode\bin\code.cmd" "F:\Apps\freedom_system"
goto done

:launch_terminal
setlocal enabledelayedexpansion
echo.
echo ========================================
echo  Select working directory:
echo ========================================
echo.
echo   1. F:\Apps\freedom_system
set "DIR_1=F:\Apps\freedom_system"
set "DIR_COUNT=1"

for /d %%D in ("F:\Apps\freedom_system\REPO_*") do (
    set /a DIR_COUNT+=1
    echo   !DIR_COUNT!. %%~nxD
    set "DIR_!DIR_COUNT!=%%D"
)

echo.
set "DIR_CHOICE="
set /p "DIR_CHOICE=Enter choice (1-!DIR_COUNT!): "
if not defined DIR_CHOICE set "DIR_CHOICE=1"
call set "SELECTED_DIR=%%DIR_!DIR_CHOICE!%%"
if not defined SELECTED_DIR (
    echo Invalid choice. Defaulting to F:\Apps\freedom_system
    set "SELECTED_DIR=F:\Apps\freedom_system"
)

echo.
echo Opening Terminal in: !SELECTED_DIR!

REM --- Extract folder name from selected directory ---
for %%F in ("!SELECTED_DIR!") do set "FOLDER_NAME=%%~nxF"

REM --- Capture all cmd.exe command lines for searching ---
set "WMIC_DUMP="
for /f "usebackq skip=1 tokens=*" %%L in (`wmic process where "name='cmd.exe'" get commandline 2^>nul`) do (
    set "WMIC_DUMP=!WMIC_DUMP! %%L"
)

REM --- Find first available instance number (01-99) ---
set "NEXT_NUM="
for /l %%I in (1,1,99) do (
    if not defined NEXT_NUM (
        if %%I lss 10 (set "TEST_NUM=0%%I") else (set "TEST_NUM=%%I")
        echo !WMIC_DUMP! | findstr /i /c:"title !FOLDER_NAME!_!TEST_NUM!" >nul 2>&1
        if errorlevel 1 set "NEXT_NUM=!TEST_NUM!"
    )
)
if not defined NEXT_NUM set "NEXT_NUM=01"

set "TERMINAL_NAME=!FOLDER_NAME!_!NEXT_NUM!"
echo  Terminal name: !TERMINAL_NAME!

endlocal & set "SELECTED_DIR=%SELECTED_DIR%" & set "TERMINAL_NAME=%TERMINAL_NAME%"

start "%TERMINAL_NAME%" cmd /k "title %TERMINAL_NAME% && cd /d %SELECTED_DIR% && echo. && echo  Claude Code Voice Mode is ready. && echo  Terminal: %TERMINAL_NAME% && echo  AllTalk TTS: http://127.0.0.1:7851 && echo  Whisper STT: http://127.0.0.1:8787 && echo. && echo  Type your commands below. && echo."
goto done

:done
echo.
echo ========================================
echo  Claude Code Voice Mode is running.
echo  Use the Mic Panel to manage services
echo  (restart, shutdown, open new terminals).
echo ========================================
echo.
echo This launcher window can be closed safely.
echo Services are managed by the Mic Panel.
echo.
pause
