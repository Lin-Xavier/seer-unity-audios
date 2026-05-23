@echo off
REM ============================================================
REM  Seer BGM Extractor - Windows build script
REM
REM  Usage: double-click this file, or run from a terminal:
REM      build.bat
REM
REM  Requirements:
REM      - Windows 10/11
REM      - Python 3.10+ installed and on PATH (python.org installer
REM        with "Add python.exe to PATH" ticked works)
REM
REM  Output: dist\SeerBGMExtractor.exe (single-file, no console window)
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo === Seer BGM Extractor: build ===
echo.

REM --- Check Python ---
where python >nul 2>&1
if errorlevel 1 (
    echo [!] python.exe not found on PATH.
    echo     Install Python 3.10+ from https://www.python.org/downloads/
    echo     and tick "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version') do echo Using %%v

REM --- Install/upgrade deps ---
echo.
echo === Installing dependencies ===
python -m pip install --upgrade pip
if errorlevel 1 goto :fail
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail
python -m pip install pyinstaller Pillow
if errorlevel 1 goto :fail

REM --- Generate the app icon (if missing or outdated) ---
echo.
echo === Generating app icon ===
python make_icon.py
if errorlevel 1 goto :fail

REM --- Build ---
echo.
echo === Running PyInstaller ===
python -m PyInstaller --noconfirm --clean ^
    --name "SeerBGMExtractor" ^
    --onefile ^
    --windowed ^
    --icon "app_icon.ico" ^
    --add-data "app_icon.ico;." ^
    --collect-all UnityPy ^
    --collect-all fmod_toolkit ^
    --collect-all pyfmodex ^
    --collect-all archspec ^
    --collect-all astc_encoder ^
    --collect-all etcpak ^
    --collect-all texture2ddecoder ^
    --collect-all albi0 ^
    --collect-submodules albi0.plugins ^
    --hidden-import albi0.plugins.newseer ^
    --hidden-import albi0.plugins.seerproject ^
    --collect-all fsb5 ^
    seer_bgm_gui.py
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo  Build complete.
echo  Output: %CD%\dist\SeerBGMExtractor.exe
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo [!] Build failed. Scroll up for the error.
pause
exit /b 1
