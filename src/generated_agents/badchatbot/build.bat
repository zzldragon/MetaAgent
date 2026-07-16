@echo off
REM Clean-venv build: only requirements.txt gets bundled.
REM (falls back to virtualenv for Pythons without the venv module)
if not exist .buildenv\Scripts\python.exe python -m venv .buildenv 2>nul
if not exist .buildenv\Scripts\python.exe python -m pip install virtualenv
if not exist .buildenv\Scripts\python.exe python -m virtualenv .buildenv
.buildenv\Scripts\python -m pip install --upgrade pip
.buildenv\Scripts\python -m pip install -r requirements.txt pyinstaller
REM --onedir (a folder, not one giant exe): faster cold start,
REM and config/state files sit visibly next to the exe.
.buildenv\Scripts\python -m PyInstaller --onedir --noconfirm --name badchatbot agent.py
copy /Y config.json dist\badchatbot\ >nul
.buildenv\Scripts\python -m PyInstaller --onedir --windowed --noconfirm --name badchatbot_gui gui.py
copy /Y config.json dist\badchatbot_gui\ >nul
echo.
echo Done. Run dist\badchatbot\badchatbot.exe or dist\badchatbot_gui\badchatbot_gui.exe (config.json already copied next to it).
pause
