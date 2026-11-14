@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Virtual environment not found. Run "%~dp0setup-env.bat" first. 1>&2
    exit /b 1
)
pushd "%~dp0" || exit /b 1
"%~dp0.venv\Scripts\python.exe" -m database_navigator.ui %*
set "LAUNCH_STATUS=%ERRORLEVEL%"
popd
exit /b %LAUNCH_STATUS%
