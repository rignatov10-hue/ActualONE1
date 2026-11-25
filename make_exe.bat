@echo off
rem Checks for Python, installs dependencies, and builds dist\Analyst.exe.
rem Run this ONCE on a Windows PC: double-click, wait, then the built EXE no
rem longer needs Python and can be copied anywhere.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install it from https://www.python.org/downloads/
  echo and during setup CHECK "Add python.exe to PATH". Then run this again.
  pause
  exit /b 1
)

echo Installing dependencies (pandas, pyarrow, openpyxl, pillow, pyinstaller)...
python -m pip install --upgrade pip >nul
python -m pip install pandas pyarrow openpyxl pillow pyinstaller
if errorlevel 1 (
  echo Failed to install dependencies. Check your internet connection.
  pause
  exit /b 1
)

echo Installing optional interactive browser support (Playwright)...
python -m pip install playwright >nul 2>nul
python -m playwright install chromium >nul 2>nul
if errorlevel 1 echo WARNING: Playwright not installed - browser tools will ask the agent to show install steps.
python -c "from playwright.sync_api import sync_playwright" >nul 2>nul
if errorlevel 1 echo WARNING: Playwright missing - browser tools disabled until installed.


echo Building Analyst.exe (takes a few minutes)...
python -m PyInstaller --noconfirm --onefile --windowed --name Analyst gui_entry.py
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo Done! The program is at:  dist\Analyst.exe
echo Copy it to any folder; settings (gui_config.json) and memory (memory\)
echo are created next to it on first run. Python is no longer needed.
pause
