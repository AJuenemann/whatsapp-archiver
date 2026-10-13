FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin archive \
    && mkdir -p /data/incoming /data/media \
    && chown -R archive:archive /data

ENV DATA_DIR=/data \
    DATABASE_URL=sqlite:////data/archive.db \
    INCOMING_DIR=/data/incoming \
    MEDIA_DIR=/data/media \
    TMPDIR=/data/tmp \
    HOME=/tmp

USER archive
EXPOSE 8000
CMD ["uvicorn", "whatsapp_archive.app:app", "--host", "0.0.0.0", "--port", "8000"]