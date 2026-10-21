@echo off
cd /d C:\Scripts

echo === GitHub Check ===
git fetch
git status -sb

echo.
echo === Latest Local ===
git log -1 --oneline

echo.
echo === Latest GitHub ===
git log origin/main -1 --oneline

pause
