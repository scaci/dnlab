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
    && rm -rf /tmp/dnlab-wheels \
    && mkdir -p /usr/share/doc/dnlab/third-party \
    && printf '%s\n' \
        '# dNLab Third-Party Notices' \
        '' \
        'This image contains dNLab multinode services plus Debian packages, Docker CLI packages and Python dependencies.' \
        'Release builds must attach a generated SBOM and full notice bundle for the exact image tag.' \
        'Containerlab is normally bind-mounted from the host rather than copied into this image.' \
        > /usr/share/doc/dnlab/third-party/THIRD_PARTY_NOTICES.md

EXPOSE 8081
CMD ["dnlab-multinode-api"]
