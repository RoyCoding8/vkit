FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ARG UBUNTU_ARCHIVE=archive.ubuntu.com
RUN sed -i "s|archive.ubuntu.com|${UBUNTU_ARCHIVE}|g" /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates libssl3t64 python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
USER 65532:65532
