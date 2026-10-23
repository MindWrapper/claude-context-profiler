"""An HTTP server for 127.0.0.1 that starts without a DNS lookup.

The standard HTTPServer looks up its own name with socket.getfqdn() when it binds. For 127.0.0.1
that is a reverse lookup, which hangs for half a minute or more on some machines (macOS CI runners,
some Windows networks), and the proxy or the page then never reports its port. The name is never
used here, so it is set without asking.
"""

from __future__ import annotations

import socketserver
from http.server import ThreadingHTTPServer


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]
