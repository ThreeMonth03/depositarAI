FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY search/api/requirements.lock /tmp/api.lock
COPY search/web/requirements.lock /tmp/web.lock
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/api.lock -r /tmp/web.lock
COPY search/__init__.py search/serialization.py search/secrets.py /app/search/
COPY search/catalog/__init__.py search/catalog/errors.py \
    search/catalog/policy.py search/catalog/query.py /app/search/catalog/
COPY search/api/__init__.py /app/search/api/
COPY search/api/conversation/__init__.py search/api/conversation/calendar.py \
    search/api/conversation/contract.py search/api/conversation/time.py /app/search/api/conversation/
COPY search/api/retrieval/__init__.py search/api/retrieval/metadata.py \
    search/api/retrieval/schemas.py /app/search/api/retrieval/
COPY search/api/shared/__init__.py search/api/shared/admission.py \
    search/api/shared/schemas.py /app/search/api/shared/
COPY search/planning/__init__.py search/planning/clock.py \
    search/planning/contract.py search/planning/predicates.py /app/search/planning/
COPY search/http /app/search/http/
COPY search/web /app/search/web/
ARG SOURCE_REVISION=development
LABEL org.opencontainers.image.revision=$SOURCE_REVISION
LABEL io.depositar.ai.public-entry.version="2"
LABEL io.depositar.ai.conversation.version="3"
ENV SOURCE_REVISION=$SOURCE_REVISION
USER 1000:1000
CMD ["python", "-m", "uvicorn", "search.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--no-proxy-headers", "--limit-concurrency", "32", "--timeout-keep-alive", "5"]
