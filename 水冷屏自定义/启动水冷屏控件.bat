@echo off
set "APP=%~dp0dist\ValkyrieV360水冷屏控件.exe"
if exist "%APP%" (
  start "" "%APP%"
  exit /b
)
echo EXE not found. Start the source version with Python instead.
python.exe "%~dp0水冷屏控件.py"
if errorlevel 1 pause
