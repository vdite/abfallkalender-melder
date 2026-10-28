FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt tzdata
COPY app/ .
COPY assets/icon.png .

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "abfallkalender.py"]
