@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 优先用 py 启动器，其次 PATH 里的 python
set "PY=python"
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"

echo ==^> 安装打包依赖
%PY% -m pip install --upgrade --quiet pywebview pyinstaller || goto :fail

echo ==^> 打包中（约 30 秒）
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "WorkBuddySignin" ^
  --icon "assets\app.ico" ^
  --add-data "desktop\ui;ui" ^
  --paths . --paths desktop ^
  --collect-all webview --collect-all clr_loader --collect-all pythonnet ^
  --hidden-import webview.platforms.edgechromium ^
  --hidden-import webview.platforms.winforms ^
  desktop\app.py || goto :fail

echo.
echo 打包完成：dist\WorkBuddySignin.exe
pause
exit /b 0

:fail
echo.
echo 打包失败，请查看上面的报错。
pause
exit /b 1
