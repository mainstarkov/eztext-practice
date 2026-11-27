FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 textscan && mkdir /data && chown textscan /data
COPY main.py bot.py service.py miniapp.py paid_messages.py telegram_paid_worker.py telegram_qr_setup.py interactive.html dashboard.html docs.html favicon.svg favicon.ico favicon-tab.png apple-touch-icon.png payment_setup.html welcome.jpg ./
USER textscan
ENV HOST=0.0.0.0 MINIAPP_HOST=0.0.0.0 DATABASE_PATH=/data/textscan.sqlite3 PYTHONUNBUFFERED=1
EXPOSE 8080 8082
CMD ["python", "main.py"]
