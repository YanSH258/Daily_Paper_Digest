# Daily Paper Digest — 文献工作台
FROM python:3.12-slim

# Chromium（DrissionPage 浏览器渲染）+ 中文字体
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium fonts-noto-cjk curl \
    && rm -rf /var/lib/apt/lists/*

ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirement.txt .
RUN pip install --no-cache-dir -r requirement.txt

COPY setup.py ./
COPY src ./src
RUN pip install --no-cache-dir .

EXPOSE 8080
# 配置与数据通过 volume 挂载：
#   -v ./config:/app/config  -v ./data:/app/data
CMD ["daily-paper-web", "--config", "/app/config/config.yaml", "--host", "0.0.0.0", "--port", "8080"]
