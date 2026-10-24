# Requires SandboxSpec(tmp_exec=True): test binaries are built and executed from /tmp.
FROM rust:1.83-slim-bookworm@sha256:540c902e99c384163b688bbd8b5b8520e94e7731b27f7bd0eaa56ae1960627ab
# CARGO_HOME stays in the image (read-only registry-less); build output goes to tmpfs.
ENV HOME=/tmp CARGO_TARGET_DIR=/tmp/target CARGO_NET_OFFLINE=true
USER 65534:65534
