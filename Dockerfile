FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir pip==26.1.2 \
    && pip install --no-cache-dir -r requirements.txt

# Static docker CLI only (no daemon): execcheck's EXEC_SANDBOX=docker mode launches its throwaway
# sandbox containers through the host daemon (the compose optimize services mount the socket).
ARG TARGETARCH
ARG DOCKER_CLI_VERSION=27.5.1
RUN arch=$([ "${TARGETARCH:-$(dpkg --print-architecture)}" = "arm64" ] && echo aarch64 || echo x86_64) \
    && python -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/tmp/docker.tgz')" \
       "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
    && tar -xzf /tmp/docker.tgz -C /tmp docker/docker \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker.tgz /tmp/docker

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
