FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY mcp_server ./mcp_server
COPY agent ./agent
COPY optimize ./optimize
COPY ui ./ui
RUN pip install --no-cache-dir ".[optimizer,ui]"

# Pre-download the local CPU embedding model so routing stays network-free at runtime.
ARG EMBED_MODEL=BAAI/bge-small-en-v1.5
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('${EMBED_MODEL}')"

COPY skills ./skills

ENV PYTHONUNBUFFERED=1

CMD ["skill-router", "serve", "--stdio"]
