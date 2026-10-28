# Product conversation API and its isolated CI target.
FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HAYSTACK_TELEMETRY_ENABLED=false HAYSTACK_CONTENT_TRACING_ENABLED=false
COPY search/api/requirements.lock /tmp/api.lock
COPY search/api/requirements-haystack.lock /tmp/haystack.lock
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/api.lock -r /tmp/haystack.lock
COPY search/__init__.py search/serialization.py search/secrets.py \
    search/model_policy.py search/scope.py /app/search/
COPY search/http /app/search/http/
COPY search/transport /app/search/transport/
COPY search/catalog/__init__.py search/catalog/bundle.py search/catalog/codec.py \
    search/catalog/embedding.py search/catalog/errors.py search/catalog/policy.py \
    search/catalog/query.py search/catalog/resources.py /app/search/catalog/
COPY search/api/*.py /app/search/api/
COPY search/api/conversation/*.py /app/search/api/conversation/
COPY search/api/retrieval/*.py /app/search/api/retrieval/
COPY search/api/shared/*.py /app/search/api/shared/
COPY search/planning /app/search/planning/
COPY search/reranker /app/search/reranker/
COPY search/operations/__init__.py /app/search/operations/
COPY search/operations/shared/__init__.py search/operations/shared/runtime_probe.py /app/search/operations/shared/
ARG SOURCE_REVISION=development
LABEL org.opencontainers.image.revision=$SOURCE_REVISION
LABEL io.depositar.ai.conversation.version="4"
ENV SOURCE_REVISION=$SOURCE_REVISION
USER 1000:1000
CMD ["python", "-m", "uvicorn", "search.api.conversation.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--no-proxy-headers", "--limit-concurrency", "16"]

FROM runtime AS tests
USER 0:0
RUN --mount=type=bind,source=search/api/requirements-test.lock,target=/tmp/test.lock,readonly \
    pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/test.lock
USER 1000:1000

FROM runtime AS production
