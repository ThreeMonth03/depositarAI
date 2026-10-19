ARG OPENSEARCH_BASE_IMAGE=ghcr.io/codelibs/fess-opensearch:3.8.0@sha256:c59dd34da6d0c6e0117b8851ded0832f168de9b33e275e62cd3ef3139362d7fc
FROM ${OPENSEARCH_BASE_IMAGE}

USER root
COPY --chown=root:root opensearch_healthcheck.py /usr/local/bin/depositar-opensearch-healthcheck.py
# OpenSearch's startup wrapper otherwise tries to create this file under
# config. Bake an empty keystore into the immutable image so the runtime root
# filesystem can remain read-only. The upstream config contains this
# placeholder even though the canary mounts its own opensearch.yml.
RUN set -eu; \
    FESS_DICTIONARY_PATH=/usr/share/opensearch/config/dictionary/ \
      /usr/share/opensearch/bin/opensearch-keystore create >/dev/null 2>&1; \
    test -s /usr/share/opensearch/config/opensearch.keystore; \
    chown root:root /usr/share/opensearch/config/opensearch.keystore; \
    chmod 0444 /usr/share/opensearch/config/opensearch.keystore; \
    chmod 0555 /usr/local/bin/depositar-opensearch-healthcheck.py \
    && chmod 0755 /usr/share/opensearch \
    && chmod 0755 /usr/share/opensearch/plugins/opensearch-security/tools/hash.sh \
    && chmod -R a+rX /usr/share/opensearch/config

# Compose overrides this with the invoking non-root host UID/GID. Keeping the
# upstream OpenSearch UID as the image default makes direct runs non-root.
USER 1000:1000
