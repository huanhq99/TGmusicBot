FROM python:3.11-slim

WORKDIR /app

# 安装编译工具、中文字体和图片处理依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    fonts-noto-cjk \
    fonts-dejavu-core \
    fontconfig \
    ca-certificates \
    openssl \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

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
