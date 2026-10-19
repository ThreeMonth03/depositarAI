FROM nginx:1.30.4-alpine@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c
COPY deploy/config/web-nginx/nginx.conf /etc/nginx/nginx.conf
ARG SOURCE_REVISION=development
LABEL org.opencontainers.image.revision=$SOURCE_REVISION
USER 1000:1000
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
