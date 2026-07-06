FROM alpine:3.20

RUN apk add --no-cache \
      bash \
      iproute2 \
      iptables \
      nftables \
      frr \
      tcpdump

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
