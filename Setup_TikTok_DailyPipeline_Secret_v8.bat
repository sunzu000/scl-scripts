@echo off
chcp 65001 >nul
setlocal
echo Paste the NEW Supabase sb_secret_ key, then press Enter.
set /p SKEY=Secret key: 
echo %SKEY%| findstr /b /c:"sb_secret_" >nul || (echo ERROR: sb_secret_ key required.& exit /b 1)
> "C:\Scripts\TikTok_DailyPipeline.secret" echo %SKEY%
icacls "C:\Scripts\TikTok_DailyPipeline.secret" /inheritance:r >nul
icacls "C:\Scripts\TikTok_DailyPipeline.secret" /grant:r "%USERNAME%:R" "SYSTEM:R" "Administrators:R" >nul
echo New secret saved with restricted ACL.
endlocal
