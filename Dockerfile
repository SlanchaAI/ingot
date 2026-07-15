FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the CPU embedding model so the demo runs offline (no runtime download).
# Override with --build-arg EMBED_MODEL=... to bake a different fastembed model (keep it in sync
# with the EMBED_MODEL env the server reads, or the model downloads on first boot instead).
ARG EMBED_MODEL=BAAI/bge-small-en-v1.5
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('${EMBED_MODEL}')"

COPY mcp_server ./mcp_server
COPY agent ./agent
COPY optimize ./optimize
COPY ui ./ui
COPY skills ./skills

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "mcp_server.server"]
