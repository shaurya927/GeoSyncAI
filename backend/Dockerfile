FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY backend/README.md ./README.md
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
RUN groupadd --gid 10001 geosyncai && useradd --uid 10001 --gid geosyncai --no-create-home geosyncai \
    && mkdir -p /app/storage && chown geosyncai:geosyncai /app/storage
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log", "--limit-concurrency", "128"]
