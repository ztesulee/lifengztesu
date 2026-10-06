@echo off
cd /d "%~dp0"
set PHOTO_RETURN_DEBUG=1
set PYTHONUTF8=1
python -u app.py
pause
