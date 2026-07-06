# syntax=docker/dockerfile:1.7
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DNLAB_MULTINODE_API_HOST=0.0.0.0 \
    DNLAB_MULTINODE_API_PORT=8081

WORKDIR /opt/dnlab-multinode

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        docker-cli \
        docker.io \
        iproute2 \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN pip wheel --no-cache-dir --wheel-dir /tmp/dnlab-wheels ".[api]" \
    && pip install --no-cache-dir --no-index --find-links /tmp/dnlab-wheels "dnlab-multinode[api]" \
    && rm -rf /tmp/dnlab-wheels

EXPOSE 8081
CMD ["dnlab-multinode-api"]
