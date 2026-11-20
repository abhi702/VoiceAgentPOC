FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    libasound2 libssl3 ca-certificates libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app/ai-orchestrator
COPY ai-orchestrator/pyproject.toml ./
COPY ai-orchestrator/app ./app
RUN pip install --no-cache-dir .
COPY sops /app/sops
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
