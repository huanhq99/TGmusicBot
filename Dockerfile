FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用
COPY bot/ ./bot/

# 创建数据目录
RUN mkdir -p /app/data /app/uploads

# 环境变量
ENV DATA_DIR=/app/data
ENV UPLOAD_DIR=/tmp/tgmusicbot_uploads
ENV MUSIC_TARGET_DIR=/app/uploads
ENV PYTHONUNBUFFERED=1

# 暴露 Web 端口
EXPOSE 8080

# 启动脚本
COPY start.sh .
RUN chmod +x start.sh

CMD ["./start.sh"]
