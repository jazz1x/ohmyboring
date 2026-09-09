#!/usr/bin/env python3
"""Serve one fixed body on 127.0.0.1 so a gate can be tested against a shaped /health.

Extracted from test_drudge_health_readiness.sh when a second suite needed the same stub. Two
copies of a test fixture drift the way two copies of anything else do, and a drifted fixture
fails in the direction that looks like a pass.

Usage: health_stub.py '<json body>' <port>
"""

import http.server
import sys


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: health_stub.py '<body>' <port>")
    body, port = sys.argv[1].encode(), int(sys.argv[2])

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server's spelling
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
