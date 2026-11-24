@echo off
rem Send the product decisions made on this PC (search patterns + reference products) to GitHub,
rem so the daily family page can compare items you searched here for the first time.
chcp 65001 >nul
cd /d "%~dp0.."
copy /y ".il-supermarket-work\queries_cache.json" "family\data\queries_cache.json" >nul
copy /y ".il-supermarket-work\ref_products.json" "family\data\ref_products.json" >nul
git add family\data\queries_cache.json family\data\ref_products.json
git commit -m "Update product decisions from the PC" >nul 2>nul
git push
echo.
echo עודכן. העמוד המשפחתי ישתמש במוצרים החדשים מהעדכון הבא (או לחצי Run workflow ב-GitHub).
pause
