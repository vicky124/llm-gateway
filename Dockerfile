FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml ./
COPY gateway ./gateway
RUN pip install --no-cache-dir ".[redis]"
COPY config ./config

RUN useradd --create-home --uid 10001 gateway
USER gateway

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8080/healthz')"
CMD ["uvicorn", "gateway.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
