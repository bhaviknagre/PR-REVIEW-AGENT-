#!/bin/sh
# Build all sandbox images: ./docker/sandbox/build.sh  (run from the repo root)
# Note: go and rust images compile test binaries into /tmp, so run them with SandboxSpec(tmp_exec=True).
set -eu
d=$(dirname "$0")
docker build -f "$d/python.Dockerfile" -t pr-review-sandbox-python:3.12 "$d"
docker build -f "$d/node.Dockerfile"   -t pr-review-sandbox-node:20     "$d"
docker build -f "$d/go.Dockerfile"     -t pr-review-sandbox-go:1.22     "$d"
docker build -f "$d/rust.Dockerfile"   -t pr-review-sandbox-rust:1.83   "$d"
