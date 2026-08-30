# syntax=docker/dockerfile:1.7

FROM node:26-alpine AS frontend-builder
WORKDIR /src/frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
RUN mkdir -p /src/backend && npm run build

FROM golang:1.27-alpine AS backend-builder
WORKDIR /src/backend
COPY backend/go.mod backend/go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY backend/ ./
COPY --from=frontend-builder /src/backend/static ./static
ENV CGO_ENABLED=0
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go build -trimpath -ldflags="-s -w" -o /out/sunnyregister-go .

FROM debian:bookworm-slim AS runtime
LABEL org.opencontainers.image.title="SunnyRegister" \
      org.opencontainers.image.description="SunnyRegister Go backend and bundled React frontend"
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=backend-builder /out/sunnyregister-go /app/sunnyregister-go
ENV PORT=8000 \
    TZ=Asia/Shanghai
VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["/app/sunnyregister-go", "--healthcheck"]
CMD ["/app/sunnyregister-go"]
