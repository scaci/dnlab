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

EXPOSE 8080
CMD ["python", "/opt/dnlab-gui/run.py"]
