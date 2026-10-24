@echo off
setlocal EnableExtensions

if "%~1"=="" (
  echo [ERROR] Usage: RecoverTikTok.bat YYYYMMDD
  exit /b 1
)

set "DATE=%~1"
set "ROOT=F:\SCL\TikTok\%DATE:~0,4%\%DATE:~0,6%"
set "HB=%ROOT%\%DATE%_HB.mp4"
set "LIVE=%ROOT%\%DATE%_LIVE"
set "PC=%LIVE%\%DATE%_PC.mp4"
set "PY=F:\SCL\TOOLS\whisper-env\Scripts\python.exe"
set "PIPE=C:\Scripts\digest_pipeline.py"
set "CFG=C:\Scripts\digest_config.json"

if not exist "%HB%" (
  echo [ERROR] HB file not found:
  echo %HB%
  exit /b 2
)

if not exist "%LIVE%" mkdir "%LIVE%"

if not exist "%PC%" (
  echo [COPY] %HB%
  echo     -^> %PC%
  copy /y "%HB%" "%PC%" >nul
  if errorlevel 1 (
    echo [ERROR] Copy failed.
    exit /b 3
  )
) else (
  echo [SKIP] PC.mp4 already exists.
)

echo [TRANSCRIBE] %DATE%
"%PY%" "%PIPE%" --output "%LIVE%" --config "%CFG%" --date "%DATE%" --transcribe-only
if errorlevel 1 (
  echo [ERROR] Transcription failed.
  exit /b 4
)

if not exist "%LIVE%\transcript.json" (
  echo [ERROR] transcript.json was not created.
  exit /b 5
)

echo.
echo [OK] Recovery transcription completed.
echo %LIVE%\transcript.json
echo.
echo Next: create playlist.json, then run:
echo C:\Scripts\MakeDigest.bat %DATE%

endlocal
exit /b 0
