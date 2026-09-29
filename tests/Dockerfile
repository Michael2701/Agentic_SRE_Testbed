FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=10

WORKDIR /tests

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY load.py conftest.py ./
COPY integration ./integration
COPY faults ./faults

# Integration first: fault tests change system state (each one recovers before the next).
CMD ["pytest", "-v", "-p", "no:cacheprovider", "integration", "faults"]
