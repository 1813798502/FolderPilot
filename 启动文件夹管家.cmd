@echo off
if not exist "D:\python\pythonw.exe" (
  echo Python was not found at D:\python\pythonw.exe
  pause
  exit /b 1
)
start "FolderPilot" "D:\python\pythonw.exe" "%~dp0folder_pilot.py"
