@echo off
REM Run the Seer BGM Extractor GUI from source (no PyInstaller build).
REM Handy for testing changes before producing the .exe.

setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [!] python.exe not found on PATH.
    pause
    exit /b 1
)

REM Quietly ensure deps are present
python -m pip install -q -r requirements.txt

python seer_bgm_gui.py
