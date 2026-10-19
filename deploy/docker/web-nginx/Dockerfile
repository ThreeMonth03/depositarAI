FROM node:24.14.0-bookworm-slim@sha256:d8e448a56fc63242f70026718378bd4b00f8c82e78d20eefb199224a4d8e33d8 AS assets
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY web/index.html web/tsconfig.json web/vite.config.ts ./
COPY web/src ./src
RUN npm run build

FROM nginx:1.30.4-alpine@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c
COPY deploy/config/web-nginx/nginx.conf deploy/config/web-nginx/http.conf deploy/config/web-nginx/website.conf /etc/nginx/
COPY --from=assets /web/dist /app/web-dist/
ARG SOURCE_REVISION=development
LABEL org.opencontainers.image.revision=$SOURCE_REVISION
LABEL io.depositar.ai.frontend.version="1"
USER 1000:1000
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
