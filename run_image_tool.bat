@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM 批量图片 AI 去水印工具启动脚本
REM 功能：
REM 1. 自动进入当前 bat 所在目录。
REM 2. 如果不存在 .venv，则自动创建虚拟环境。
REM 3. 自动启用虚拟环境，并把 requirements.txt 安装到 .venv 中。
REM 4. 如果不存在 .env，则从 .env.example 复制一份并提示先填写 API 密钥。
REM 5. 双击本文件即可运行 MyImageTool.py，不污染系统主 Python 环境。
REM ============================================================

cd /d "%~dp0"

set "VENV_DIR=%~dp0.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "ACTIVATE_BAT=%VENV_DIR%\Scripts\activate.bat"

if not exist "%PYTHON_EXE%" (
    echo [环境] 未检测到虚拟环境，正在创建 .venv ...
    py -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败，请确认已经安装 Python，并且 py 命令可用。
        pause
        exit /b 1
    )
)

call "%ACTIVATE_BAT%"
if errorlevel 1 (
    echo [错误] 启用虚拟环境失败：%ACTIVATE_BAT%
    pause
    exit /b 1
)

echo [环境] 当前使用的 Python：
python -c "import sys; print(sys.executable)"

echo [依赖] 正在检查并安装 requirements.txt 到虚拟环境 ...
python -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络或 requirements.txt。
    pause
    exit /b 1
)

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [配置] 已从 .env.example 创建 .env。
        echo [配置] 请在打开的记事本中填写 VOLC_ACCESSKEY 和 XAI_API_KEY，然后再次双击运行本 bat。
        notepad ".env"
        pause
        exit /b 0
    ) else (
        echo [错误] 未找到 .env 或 .env.example，无法加载 API 密钥配置。
        pause
        exit /b 1
    )
)

if not exist "input" mkdir "input"
if not exist "output" mkdir "output"

echo [运行] 启动批量图片 AI 去水印工具 ...
echo [提示] 默认读取 input 文件夹，输出到 output 文件夹。
echo.
python MyImageTool.py --input input --output output
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo [完成] 脚本运行结束。
) else (
    echo [结束] 脚本返回错误码：%EXIT_CODE%。请查看上方日志。
)

pause
exit /b %EXIT_CODE%
