@echo off
rem Build the family page on this PC (all chains answer from Israel) and publish it:
rem   1. family\build_regions.py  - price data for the areas in family\regions.json (free search)
rem   2. family\daily_page.py     - the page + the saved lists
rem   3. push site\ to the "pages" branch, replacing it (no history: the data is large); the GitHub
rem      workflow publishes that branch as is.
rem Run every 3 days by the Windows task "SupermarketFamilyPage"; can also be double-clicked.
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set "PYTHONIOENCODING=utf-8"
set "IL_SUPERMARKET_PLUGIN_DIR=%~dp0..\vendor\il-supermarket-compare"
if not exist ".il-supermarket-work" mkdir ".il-supermarket-work"
set "LOG=.il-supermarket-work\publish.log"
echo [%date% %time%] start >> "%LOG%"
python -B family\build_regions.py >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] area data failed >> "%LOG%"
  exit /b 1
)
python -B family\daily_page.py --publish >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] page build failed >> "%LOG%"
  exit /b 1
)
for /f "delims=" %%u in ('git remote get-url origin') do set "ORIGIN=%%u"
pushd site
if exist ".git" rmdir /s /q ".git"
rem never publish a build in progress
(echo data.build/& echo data.old/)> .gitignore
git init -q -b pages
git add -A
git -c user.name="supermarket-app" -c user.email="noreply@localhost" commit -q -m "Family page from the PC"
git push -f "%ORIGIN%" pages >> "..\%LOG%" 2>&1
set "RC=%errorlevel%"
rmdir /s /q ".git"
popd
rem tell GitHub to publish it (the workflow runs from main)
echo %date% %time% > family\last_publish.txt
git add family\last_publish.txt >> "%LOG%" 2>&1
git -c user.name="supermarket-app" -c user.email="noreply@localhost" commit -q -m "Publish the family page" >> "%LOG%" 2>&1
git push >> "%LOG%" 2>&1
echo [%date% %time%] done (pages push exit %RC%) >> "%LOG%"
endlocal
