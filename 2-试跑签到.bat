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

echo 正在执行：查状态 -^> 领取 -^> 复核
echo.
"%PYEXE%" %PYARG% -X utf8 wb_checkin.py
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo [OK] 成功，或今天已经签过。
) else (
    echo [FAIL] 退出码 %RC%
    echo        3 = 登录态过期，重新双击 1-提取凭证.bat
    echo        4 = 网络/服务端异常，稍后重试
    echo        5 = 业务失败，看上面日志里的 code 和 msg
)
pause
exit /b %RC%

:nopy
echo [ERROR] 没找到 Python。请先安装 Python 3.9+（安装时勾选 Add python.exe to PATH）
echo         下载： https://www.python.org/downloads/
pause
exit /b 2
