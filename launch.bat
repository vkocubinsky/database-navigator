@echo off
rem Copyright (C) 2026 vkocubinsky
rem SPDX-License-Identifier: GPL-3.0-or-later
rem
rem This program is free software: you can redistribute it and/or modify
rem it under the terms of the GNU General Public License as published by
rem the Free Software Foundation, either version 3 of the License, or
rem (at your option) any later version.
rem
rem This program is distributed in the hope that it will be useful,
rem but WITHOUT ANY WARRANTY; without even the implied warranty of
rem MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
rem GNU General Public License for more details.
rem
rem You should have received a copy of the GNU General Public License
rem along with this program. If not, see <https://www.gnu.org/licenses/>.

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
