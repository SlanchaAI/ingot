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

# Pre-download the CPU embedding models so the demo runs offline (no runtime download):
# the Qwen3 q4 ONNX router default (~925MB including its tokenizer), plus the bge-small fastembed
# fallback (~34MB), so an EMBED_MODEL override to the previous default also works offline. Keep
# these in sync with the
# EMBED_MODEL / EMBED_ONNX_FILE envs the server reads, or the model downloads on first boot.
ARG EMBED_MODEL=onnx-community/Qwen3-Embedding-0.6B-ONNX
ARG EMBED_ONNX_FILE=onnx/model_q4.onnx
RUN python -c "from huggingface_hub import hf_hub_download as fetch; \
fetch('${EMBED_MODEL}', 'tokenizer.json'); fetch('${EMBED_MODEL}', '${EMBED_ONNX_FILE}')"
ENV BAKED_EMBED_MODEL=${EMBED_MODEL} \
    BAKED_EMBED_ONNX_FILE=${EMBED_ONNX_FILE}
ARG FALLBACK_EMBED_MODEL=BAAI/bge-small-en-v1.5
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('${FALLBACK_EMBED_MODEL}')"
ENV BAKED_FALLBACK_EMBED_MODEL=${FALLBACK_EMBED_MODEL}

COPY mcp_server ./mcp_server
COPY agent ./agent
COPY optimize ./optimize
COPY ui ./ui
COPY skills ./skills

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "mcp_server.server"]
