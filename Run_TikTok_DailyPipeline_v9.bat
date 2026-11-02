@echo off
setlocal
cd /d C:\Scripts
py -3 TikTok_DailyPipeline_v9.py
set RC=%ERRORLEVEL%
echo.
echo EXIT_CODE=%RC%
exit /b %RC%
