@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set PHOTO_RETURN_DEBUG=1
python -u lightroom_bridge.py
pause
