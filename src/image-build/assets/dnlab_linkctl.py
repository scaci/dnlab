#!/usr/bin/env python3
"""Set the QEMU carrier state of one pre-provisioned vrnetlab interface."""

import socket
import sys


SOCKET_PATH = "/run/dnlab-link-control.sock"


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: dnlab-linkctl ethN up|down", file=sys.stderr)
        return 2
    request = f"{sys.argv[1]} {sys.argv[2]}\n".encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(SOCKET_PATH)
            client.sendall(request)
            response = client.recv(4096).decode(errors="replace").strip()
    except OSError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    stream = sys.stdout if response.startswith("OK ") else sys.stderr
    print(response, file=stream)
    return 0 if response.startswith("OK ") else 1


if __name__ == "__main__":
    raise SystemExit(main())
