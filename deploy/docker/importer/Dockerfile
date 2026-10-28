ARG SEARCH_CATALOG_ADMIN_BASE_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
FROM ${SEARCH_CATALOG_ADMIN_BASE_IMAGE}

WORKDIR /opt/depositar-search
COPY --chown=root:root search/__init__.py search/serialization.py \
    search/catalog_admin.py search/vector_journal.py ./search/
COPY --chown=root:root search/catalog/__init__.py search/catalog/errors.py \
    search/catalog/policy.py search/catalog/codec.py search/catalog/transport.py \
    search/catalog/session.py search/catalog/admin.py search/catalog/bundle.py \
    search/catalog/inventory.py search/catalog/importer.py search/catalog/journal.py \
    search/catalog/vectors.py search/catalog/resources.py ./search/catalog/
COPY --chown=root:root search/operations/__init__.py ./search/operations/
COPY --chown=root:root search/operations/shared/__init__.py \
    search/operations/shared/private_io.py ./search/operations/shared/
RUN mkdir -p /catalog /run/secrets \
    && chmod 0555 /catalog /run/secrets \
    && find /opt/depositar-search/search -type d -exec chmod 0555 {} + \
    && find /opt/depositar-search/search -type f -exec chmod 0444 {} +

USER 65534:65534
ENTRYPOINT ["python3", "-E", "-s", "-B", "-u", "-m", "search.catalog_admin"]
