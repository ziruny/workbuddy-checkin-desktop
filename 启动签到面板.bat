@echo off
rem WorkBuddy 签到面板 · 双击启动（优先用打包好的 exe，没有就用本机 Python 跑源码）
setlocal
set "APPDIR=%~dp0"

if exist "%APPDIR%dist\WorkBuddy签到.exe" (
  start "" "%APPDIR%dist\WorkBuddy签到.exe"
  exit /b 0
)
if exist "%APPDIR%dist\WorkBuddySignin.exe" (
  start "" "%APPDIR%dist\WorkBuddySignin.exe"
  exit /b 0
)

where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw "%APPDIR%desktop\app.py"
  exit /b 0
)

where py >nul 2>nul
if not errorlevel 1 (
  start "" py -3w "%APPDIR%desktop\app.py"
  exit /b 0
)

echo 没有找到打包好的 exe，也没有找到 Python。
echo   1) 双击 打包exe.bat 生成免环境的 exe（需先装 Python 3.9+）
echo   2) 或安装 Python 3.9+ 后执行: pip install pywebview
pause
exit /b 1
