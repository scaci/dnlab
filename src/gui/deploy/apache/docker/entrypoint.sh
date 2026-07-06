#!/bin/sh
set -eu

if [ "${DNLAB_PROXY_MODE:-tls}" = "tls" ]; then
    : "${DNLAB_PROXY_SERVER_NAME:?set DNLAB_PROXY_SERVER_NAME for tls mode}"
    export DNLAB_PROXY_WEBUI_SUFFIX="${DNLAB_PROXY_WEBUI_SUFFIX:-$DNLAB_PROXY_SERVER_NAME}"
    if [ "$DNLAB_PROXY_WEBUI_SUFFIX" != "$DNLAB_PROXY_SERVER_NAME" ]; then
        echo "DNLAB_PROXY_WEBUI_SUFFIX must match DNLAB_PROXY_SERVER_NAME" >&2
        exit 2
    fi
    export DNLAB_PROXY_SERVER_ADMIN="${DNLAB_PROXY_SERVER_ADMIN:-dnlab@example.invalid}"
    export DNLAB_PROXY_CERT_FILE="${DNLAB_PROXY_CERT_FILE:-/etc/ssl/dnlab/dnlab-gui.crt}"
    export DNLAB_PROXY_CERT_KEY_FILE="${DNLAB_PROXY_CERT_KEY_FILE:-/etc/ssl/dnlab/dnlab-gui.key}"

    envsubst '${DNLAB_PROXY_SERVER_NAME} ${DNLAB_PROXY_WEBUI_SUFFIX} ${DNLAB_PROXY_SERVER_ADMIN} ${DNLAB_PROXY_CERT_FILE} ${DNLAB_PROXY_CERT_KEY_FILE}' \
        < /etc/apache2/templates/dnlab-gui-prod.conf.template \
        > /etc/apache2/sites-available/000-dnlab-gui.conf
fi

exec "$@"
