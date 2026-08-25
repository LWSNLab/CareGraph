# Multi-stage build for the Go API gateway.
# Requires go.sum to exist — run `go mod tidy` before building.

FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /out/api ./cmd/api

# Issuing and listing API keys is the one routine operator task on a server, and
# every /v1 route needs a key — so an image without this leaves a deployment that
# nobody can use unless a Go toolchain is installed alongside it, which is exactly
# what this image exists to avoid. Reached with `--entrypoint /apikey`.
RUN CGO_ENABLED=0 go build -trimpath -o /out/apikey ./cmd/apikey

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/api /api
COPY --from=build /out/apikey /apikey
EXPOSE 8080
USER nonroot:nonroot

# The binary probes itself: this image has no shell for a healthcheck to use.
# /readyz rather than /healthz, because a healthcheck gates depends_on and
# load-balancer membership — "can this serve", not "is the process alive".
# Declared here rather than in compose so it travels with the image.
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD ["/api", "-healthcheck"]

ENTRYPOINT ["/api"]
