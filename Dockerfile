FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[serve]" \
    && addgroup --system --gid 10001 agentlab \
    && adduser --system --uid 10001 --ingroup agentlab agentlab

USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["agentlab", "serve", "--corpus", "/corpus", "--port", "8000", "--bind", "container"]
