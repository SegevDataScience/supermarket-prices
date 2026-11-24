@echo off
setlocal EnableExtensions
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title השוואת מחירי סופר - אל תסגור
rem Turn off QuickEdit so a click in this window can't freeze the server.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0disable_quickedit.ps1" >nul 2>nul

set "IL_SUPERMARKET_PLUGIN_DIR=%~dp0vendor\il-supermarket-compare"
rem Model for the search job: sonnet is 2-3x faster than opus.
set "SUPERMARKET_MODEL=sonnet"
if not exist "%~dp0.il-supermarket-work" mkdir "%~dp0.il-supermarket-work"
set "RESTART_LOG=%~dp0.il-supermarket-work\restarts.log"
rem The browser opens only on the first start; restarts set this to 1.
set "SUPERMARKET_NO_BROWSER="

:check
where python >nul 2>nul
if errorlevel 1 goto missing_python
where claude >nul 2>nul
if errorlevel 1 goto missing_claude
goto loop

:missing_python
echo [%date% %time%] לא נמצא Python ב-PATH. התקן Python 3 והוסף אותו ל-PATH. בודק שוב בעוד 10 שניות...
ping -n 11 127.0.0.1 >nul
goto check

:missing_claude
echo [%date% %time%] לא נמצא Claude Code ב-PATH. התקן את Claude Code והתחבר לחשבון שלך. בודק שוב בעוד 10 שניות...
ping -n 11 127.0.0.1 >nul
goto check

:loop
echo [%date% %time%] מפעיל את השרת...
python app.py
set "EXITCODE=%errorlevel%"
rem 130 = stopped with Ctrl+C on purpose: don't restart.
if "%EXITCODE%"=="130" goto stopped
rem 3 = reviewed code changed (checksums.txt): restarting won't help.
if "%EXITCODE%"=="3" goto integrity
echo.
echo [%date% %time%] השרת נעצר עם קוד יציאה %EXITCODE%. מפעיל מחדש בעוד 3 שניות...
>>"%RESTART_LOG%" echo [%date% %time%] exit code %EXITCODE%, restarting
set "SUPERMARKET_NO_BROWSER=1"
ping -n 4 127.0.0.1 >nul
goto loop

:stopped
echo [%date% %time%] השרת נעצר ^(Ctrl+C^).
goto :eof

:integrity
echo.
echo [%date% %time%] השרת לא יופעל עד שהקבצים שלמעלה ייבדקו מחדש. לא מפעיל מחדש.
>>"%RESTART_LOG%" echo [%date% %time%] exit code 3: reviewed code changed, not restarting
pause
exit /b 3
