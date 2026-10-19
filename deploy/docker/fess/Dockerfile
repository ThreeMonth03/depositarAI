ARG FESS_BASE_IMAGE=ghcr.io/codelibs/fess:15.8.0@sha256:442a58831e87aae6ae22ef60e0536ececbda553bd5f5e8e5dc98cc022578dc75
FROM ${FESS_BASE_IMAGE}

ARG FESS_OLLAMA_PLUGIN_URL
ARG FESS_OLLAMA_PLUGIN_SHA256

USER root
RUN set -eu; \
    test "$FESS_OLLAMA_PLUGIN_URL" = "https://maven.codelibs.org/release/org/codelibs/fess/fess-llm-ollama/15.8.0/fess-llm-ollama-15.8.0.jar"; \
    test "$FESS_OLLAMA_PLUGIN_SHA256" = "08301d94101404ea8fd4ae9efb12cb61cf7d2f28b554653a1b65ee5233d39b84"; \
    target=/usr/share/fess/app/WEB-INF/plugin/fess-llm-ollama-15.8.0.jar; \
    curl --disable --fail --silent --show-error --location --max-redirs 3 \
      --proto '=https' --proto-redir '=https' --tlsv1.2 \
      --connect-timeout 10 --max-time 120 --max-filesize 10485760 \
      --output "$target" "$FESS_OLLAMA_PLUGIN_URL"; \
    printf '%s  %s\n' "$FESS_OLLAMA_PLUGIN_SHA256" "$target" | sha256sum -c -; \
    chown root:root "$target"; \
    chmod 0444 "$target"

# Fess seeds a new fess_config.scheduled_job index from this classpath
# resource.  The checked-in copy is the 14-job Fess 15.8.0 upstream seed with
# every job unavailable and every cron expression empty.  Existing indices
# are disabled again by the entrypoint before the Fess JVM is allowed to run.
COPY --chown=root:root deploy/config/fess/scheduled_job.bulk /usr/share/fess/app/WEB-INF/classes/fess_indices/fess_config.scheduled_job/scheduled_job.bulk
RUN set -eu; \
    target=/usr/share/fess/app/WEB-INF/classes/fess_indices/fess_config.scheduled_job/scheduled_job.bulk; \
    printf '%s  %s\n' \
      abb580576f1f24a767ec32079fdb1a4b84b9d26281f8f923c1d7be3ac6723653 \
      "$target" | sha256sum -c -; \
    chown root:root "$target"; \
    chmod 0444 "$target"

COPY --chown=root:root deploy/docker/fess/entrypoint.sh /usr/local/bin/depositar-fess-entrypoint
COPY --chown=root:root deploy/docker/fess/fess_healthcheck.sh /usr/local/bin/depositar-fess-healthcheck
RUN chmod 0555 \
      /usr/local/bin/depositar-fess-entrypoint \
      /usr/local/bin/depositar-fess-healthcheck

# Compose overrides this with the invoking non-root host UID/GID. Keeping the
# upstream Fess UID as the image default makes accidental direct runs non-root.
USER 1001:1001
ENTRYPOINT ["/usr/local/bin/depositar-fess-entrypoint"]
