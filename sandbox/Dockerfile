# Образ песочницы для кода Эли (SANDBOX_BACKEND=docker). Собирается отдельно от бота:
#   docker build -t elya-sandbox sandbox
# Каждый запуск — новый контейнер без сети, только для чтения, от пользователя nobody.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 MPLBACKEND=Agg

# Шрифт DejaVu — подписи графиков на русском.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt seaborn==0.13.2 pillow==13.0.0 \
    && rm /tmp/requirements.txt

USER 65534:65534
WORKDIR /work
