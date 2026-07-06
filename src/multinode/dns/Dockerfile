FROM alpine:latest

RUN apk add --no-cache dnsmasq bash \
    && mkdir -p /usr/share/doc/dnlab/third-party \
    && printf '%s\n' \
        '# dNLab Third-Party Notices' \
        '' \
        'This image contains the dNLab DNS helper plus Alpine Linux packages including dnsmasq and bash.' \
        'Release builds must attach a generated SBOM and full notice bundle for the exact image tag.' \
        > /usr/share/doc/dnlab/third-party/THIRD_PARTY_NOTICES.md

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 53/udp 53/tcp
ENTRYPOINT ["/entrypoint.sh"]
