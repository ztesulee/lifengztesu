@echo off
cd /d "%~dp0"
pythonw app.py
if errorlevel 1 (
  echo Please install Python 3.9+ and dependencies in requirements.txt.
  pause
)
