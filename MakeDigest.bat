@echo off
setlocal

if "%~1"=="" (
    echo [ERROR] Date is required. Example: MakeDigest.bat 20261020
    exit /b 1
)

set "DATE=%~1"
set "YEAR=%DATE:~0,4%"
set "YM=%DATE:~0,6%"
set "FOLDER=F:\SCL\TikTok\%YEAR%\%YM%\%DATE%_LIVE"
set "PLAYLIST=%FOLDER%\playlist.json"

if not exist "%FOLDER%\" (
    echo [ERROR] LIVE folder not found:
    echo %FOLDER%
    exit /b 1
)

if not exist "%PLAYLIST%" (
    echo [ERROR] playlist.json not found:
    echo %PLAYLIST%
    exit /b 1
)

echo [CHECK] playlist.json

python -c "import json,sys; p=r'%PLAYLIST%'; d=json.load(open(p,encoding='utf-8-sig')); assert isinstance(d,list) and d,'playlist is empty'; [(x['title'],x['start'],x['seconds']) for x in d]; print('[OK] playlist:',len(d),'songs')" || (
    echo [ERROR] playlist.json is invalid.
    echo Required fields: title / start / seconds
    exit /b 1
)

if exist "%FOLDER%\%DATE%_digest.mp4" (
    echo [OK] Digest already exists:
    echo %FOLDER%\%DATE%_digest.mp4
    echo [SKIP] Render was not executed.
    exit /b 0
)

echo [START] Digest render
python "C:\Scripts\digest_pipeline.py" --output "%FOLDER%" --date "%DATE%" --render-only

if errorlevel 1 (
    echo [ERROR] Digest render failed.
    exit /b 1
)

echo [OK] Digest completed:
echo %FOLDER%\%DATE%_digest.mp4

endlocal

