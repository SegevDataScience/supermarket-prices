@echo off
rem Build the full family page on this PC (all chains are reachable from Israel) and send it to
rem GitHub, which publishes it as is. Run every morning by the Windows task "SupermarketFamilyPage";
rem can also be double-clicked. Output goes to .il-supermarket-work\publish.log
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set "PYTHONIOENCODING=utf-8"
set "IL_SUPERMARKET_PLUGIN_DIR=%~dp0..\vendor\il-supermarket-compare"
if not exist ".il-supermarket-work" mkdir ".il-supermarket-work"
set "LOG=.il-supermarket-work\publish.log"
echo [%date% %time%] start >> "%LOG%"
python -B family\daily_page.py --publish >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] page build failed >> "%LOG%"
  exit /b 1
)
git add published family\data >> "%LOG%" 2>&1
git -c user.name="supermarket-app" -c user.email="noreply@localhost" commit -m "Family page from the PC" >> "%LOG%" 2>&1
git push >> "%LOG%" 2>&1
echo [%date% %time%] done (exit %errorlevel%) >> "%LOG%"
endlocal
