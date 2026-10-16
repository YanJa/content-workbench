@echo off
chcp 65001 >nul
title 内容运营工作台 - 一键启动

echo ============================================================
echo   内容运营工作台  一键启动
echo   Windows + uv + pnpm
echo ============================================================
echo.
echo 需要本机已装：uv、pnpm、Python 3.11+
echo 启动后访问：http://localhost:5173
echo.

set ROOT=%~dp0

REM ---------- 后端 ----------
echo [1/2] 准备后端...
cd /d "%ROOT%backend"

if not exist ".venv" (
    echo       创建虚拟环境...
    uv venv --python 3.11
)

echo       安装依赖（清华源）...
uv pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

if not exist ".env" (
    echo.
    echo       [!] 未找到 backend\.env
    echo       请复制 .env.example 为 .env，并填入 DEEPSEEK_API_KEY
    echo.
    copy .env.example .env >nul
    echo       已从 .env.example 生成 .env，请先编辑填入 API Key
    pause
)

echo       启动后端 :8000
start "CWB-Backend" cmd /k "cd /d %ROOT%backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000"

REM 等后端起来
timeout /t 6 /nobreak >nul

REM ---------- 前端 ----------
echo [2/2] 准备前端...
cd /d "%ROOT%frontend"

if not exist "node_modules" (
    echo       安装依赖...
    call pnpm install
)

echo       启动前端 :5173
start "CWB-Frontend" cmd /k "cd /d %ROOT%frontend && pnpm dev"

echo.
echo ============================================================
echo   启动完成
echo     前端   http://localhost:5173
echo     后端   http://localhost:8000/api/health
echo.
echo   两个黑色窗口分别是前后端，关掉即停止服务
echo ============================================================
echo.
timeout /t 5 /nobreak >nul
start http://localhost:5173
