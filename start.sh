#!/bin/bash
set -e

# 启动 Web 界面 (后台)
echo "Starting Web UI on port 8080..."
python -m uvicorn bot.web:app --host 0.0.0.0 --port 8080 &

# 等待 Web 启动
sleep 2

# 启动 Telegram Bot
echo "Starting Telegram Bot..."
python -m bot.main
