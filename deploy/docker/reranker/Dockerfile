# Private prototype service; pinned Pascal-compatible runtime, no notebook entrypoint.
FROM quay.io/jupyter/pytorch-notebook@sha256:d6084f229d9287b49a82558767ec55b68f9ccfd247bc42590d3f0c580f73182c
USER root
COPY search/reranker/requirements.lock /tmp/reranker-requirements.lock
RUN /opt/conda/bin/python -m pip install --no-cache-dir --no-deps --require-hashes -r /tmp/reranker-requirements.lock
WORKDIR /app
COPY search/__init__.py search/serialization.py search/secrets.py /app/search/
COPY search/reranker /app/search/reranker/
ARG SOURCE_REVISION=development
LABEL org.opencontainers.image.revision=$SOURCE_REVISION
ENV SOURCE_REVISION=$SOURCE_REVISION HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 USE_TF=0 USE_FLAX=0 TOKENIZERS_PARALLELISM=false \
    HF_HOME=/tmp/hf PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
HEALTHCHECK --interval=20s --timeout=3s --start-period=60s --retries=5 \
    CMD /opt/conda/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=2).close()"
ENTRYPOINT ["/opt/conda/bin/python", "-m", "search.reranker.server"]
