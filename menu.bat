@echo off
REM ---------------------------------------------------------------
REM  Menu interactivo del pipeline de voz (deli-anura-cdr-etl)
REM  Doble clic o:  menu.bat
REM  No consume tokens: whisper + ECAPA corren local.
REM ---------------------------------------------------------------
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -u -m cli.menu
) else (
    python -u -m cli.menu
)
echo.
pause
endlocal
