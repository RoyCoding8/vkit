FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates libssl3t64 python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
USER 65532:65532
