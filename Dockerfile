# Multi-stage build for the Go API gateway.
# Requires go.sum to exist — run `go mod tidy` before building.

FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /out/api ./cmd/api

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/api /api
EXPOSE 8080
USER nonroot:nonroot

# The binary probes itself, because this image has no shell, no curl and no wget
# for a healthcheck to invoke — /api is the only executable in it.
#
# It hits /readyz, not /healthz: a healthcheck gates `depends_on:
# service_healthy` and load-balancer membership, and what those need to know is
# "can this serve traffic", not "is the process alive". A liveness check here
# would report healthy with an unreachable database.
#
# Declared here rather than in docker-compose.yml so it travels with the image —
# anyone running it without compose gets the same check, and there is one
# definition to keep correct.
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD ["/api", "-healthcheck"]

ENTRYPOINT ["/api"]
