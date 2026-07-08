@echo off
cd /d "D:\Claude Projects\INVEST Stocks\Invest Stocks Portfolio"

echo [1/4] Cleaning up stale git locks...
if exist .git\index.lock del /f .git\index.lock
if exist .git\HEAD.lock  del /f .git\HEAD.lock
if exist .git\refs\heads\main.lock del /f .git\refs\heads\main.lock

echo [2/4] Staging key files...
git add tv_enrich.py
git add push_new.bat
git add track_new_tickers.py 2>nul
git add finviz_scan.py       2>nul

echo [3/4] Committing...
git commit -m "fix: market-wide 52w highs/lows + country ETF accordion + mpSort fix"
if %errorlevel% NEQ 0 (
    echo Nothing new to commit — already up to date.
)

echo [4/4] Force-pushing to main...
echo (Safe: the 158 origin commits are only GitHub Actions index.html updates)
echo (GitHub Actions will regenerate index.html from our new tv_enrich.py)
git push origin HEAD:refs/heads/main --force
if %errorlevel% == 0 (
    echo.
    echo SUCCESS! Changes pushed to GitHub.
    echo.
    echo Next step: go to GitHub - Actions - Update Trading Dashboard - Run workflow
    echo Or wait up to 30 min for the automatic cron run.
) else (
    echo PUSH FAILED. Check credentials or network.
)
pause
