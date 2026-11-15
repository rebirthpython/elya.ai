FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# ffmpeg — монтаж видео (/video), шрифт DejaVu — надписи и субтитры с кириллицей.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Порт берётся из PORT (его задаёт хостинг) или WEBAPP_PORT.
EXPOSE 8080
CMD ["python", "main.py"]
