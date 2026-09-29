# Build context is the repo root (see docker-compose.yml) so the shared library can be installed.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=10

WORKDIR /srv

COPY libs/observability /libs/observability
COPY libs/faultpoint /libs/faultpoint
COPY services/order/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt /libs/faultpoint "/libs/observability[http,postgres]"

COPY services/order/app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
