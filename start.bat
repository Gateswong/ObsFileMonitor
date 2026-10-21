@echo off
chcp 65001 >nul
rem 双击启动：首次运行时创建虚拟环境并安装依赖
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 正在创建虚拟环境...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)
".venv\Scripts\python.exe" -m obs_overlay %*
exit /b %errorlevel%

:error
echo 启动失败，请确认已安装 Python 3.10 或更高版本。
pause
exit /b 1
