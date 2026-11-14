@echo off
setlocal
set "PYTHON_BIN=%PYTHON%"
if not defined PYTHON_BIN set "PYTHON_BIN=python"
"%PYTHON_BIN%" -m venv "%~dp0.venv"
if errorlevel 1 exit /b 1
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 exit /b 1
echo Environment ready. Run launch.bat with your connection string and options.
