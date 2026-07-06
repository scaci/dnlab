FROM alpine:3.20

RUN apk add --no-cache \
      bash \
      iproute2 \
      frr \
      tcpdump \
    && mkdir -p /usr/share/doc/dnlab/third-party \
    && printf '%s\n' \
        '# dNLab Third-Party Notices' \
        '' \
        'This image contains the dNLab realnet route-reflector helper plus Alpine Linux packages including FRRouting.' \
        'Release builds must attach a generated SBOM and full notice bundle for the exact image tag.' \
        > /usr/share/doc/dnlab/third-party/THIRD_PARTY_NOTICES.md

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
