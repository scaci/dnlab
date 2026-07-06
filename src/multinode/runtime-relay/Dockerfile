FROM python:3.12-alpine

RUN apk add --no-cache docker-cli busybox-extras util-linux

COPY relay.py /usr/local/bin/dnlab-runtime-relay
RUN chmod +x /usr/local/bin/dnlab-runtime-relay

ENTRYPOINT ["/usr/local/bin/dnlab-runtime-relay"]
