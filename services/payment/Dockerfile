# Build context is the repo root (see docker-compose.yml) so the shared library can be installed.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=10

WORKDIR /srv

# Dependencies first (layer cached until a requirements file or a lib's pyproject changes): the libs are
# installed from a stub with just their pyproject, then their sources on top without dependencies.
COPY services/payment/requirements.txt .
COPY libs/observability/pyproject.toml /libs/observability/
RUN mkdir /libs/observability/observability \
    && pip install --no-cache-dir -r requirements.txt "/libs/observability"
COPY libs/observability /libs/observability
COPY libs/faultpoint /libs/faultpoint
RUN pip install --no-cache-dir --no-deps --force-reinstall /libs/observability /libs/faultpoint

COPY services/payment/app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
