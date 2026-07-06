FROM alpine:3.20

RUN mkdir -p /usr/share/doc/dnlab/third-party \
    && printf '%s\n' \
        '# dNLab Third-Party Notices' \
        '' \
        'This image contains the dNLab management anchor runtime plus Alpine Linux base packages.' \
        'Release builds must attach a generated SBOM and full notice bundle for the exact image tag.' \
        > /usr/share/doc/dnlab/third-party/THIRD_PARTY_NOTICES.md

CMD ["sleep", "infinity"]
