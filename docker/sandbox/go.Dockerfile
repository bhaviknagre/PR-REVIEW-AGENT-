# Requires SandboxSpec(tmp_exec=True): test binaries are built and executed from /tmp.
FROM golang:1.22.10-alpine3.20@sha256:7a1d981b1cde97beb2883022380ee0fbeb6474d3eaebd144c5b8def3e2a6e6f4
ENV HOME=/tmp GOCACHE=/opt/gocache GOPATH=/tmp/go GOFLAGS=-mod=mod GOTOOLCHAIN=local CGO_ENABLED=0
# Prewarm the build cache (std + vet) so tests fit a small tmpfs; world-writable for uid 65534.
RUN mkdir /w && cd /w && printf 'module w\n\ngo 1.22\n' > go.mod \
 && printf 'package w\nimport "testing"\nfunc TestW(t *testing.T){}\n' > w_test.go \
 && go test ./... && cd / && rm -rf /w && chmod -R a+rwX /opt/gocache
USER 65534:65534
