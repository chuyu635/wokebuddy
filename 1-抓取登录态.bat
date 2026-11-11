@echo off
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

set "PYEXE="
set "PYARG="
for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%P"
)
if not defined PYEXE (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=py"
        set "PYARG=-3"
    )
)
if not defined PYEXE (
    if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" (
        set "PYEXE=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
    )
)
if not defined PYEXE goto :nopy

echo 正在抓取 WorkBuddy 登录态...
echo.
"%PYEXE%" %PYARG% -X utf8 "tools\setup.py"
echo.
pause
exit /b 0

:nopy
echo [ERROR] 没找到 Python。请先安装 Python 3.9+（安装时勾选 Add python.exe to PATH）
echo         下载： https://www.python.org/downloads/
pause
exit /b 2
