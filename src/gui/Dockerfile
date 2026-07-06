# syntax=docker/dockerfile:1.7
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DNLABGUI_HOST=0.0.0.0 \
    DNLABGUI_PORT=8080 \
    DNLAB_MULTINODE_API_URL=http://dnlab-multinode:8081

WORKDIR /opt/dnlab-gui

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /usr/share/doc/dnlab/third-party \
    && printf '%s\n' \
        '# dNLab Third-Party Notices' \
        '' \
        'This image contains dNLab plus third-party base-image packages and Python dependencies.' \
        'Release builds must attach a generated SBOM and full notice bundle for the exact image tag.' \
        'See /opt/dnlab-gui/requirements.txt and the public dNLab THIRD_PARTY_NOTICES.md for policy details.' \
        > /usr/share/doc/dnlab/third-party/THIRD_PARTY_NOTICES.md

EXPOSE 8080
CMD ["python", "/opt/dnlab-gui/run.py"]
