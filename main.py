#!/usr/bin/env python3
"""Railway static server for the hosted (SCRUBBED) AI Build-Out app.

Inert on GitHub Pages (Pages serves index.html directly and ignores this file);
Railway's nixpacks detects it and runs the same five-file kit at a public URL.
No-store headers so a fresh push goes live immediately (the PWA service worker
adds its own stale-while-revalidate layer on top for installed home-screen apps).
"""
import http.server
import os
import socketserver

PORT = int(os.environ.get("PORT", 8080))
os.chdir(os.path.dirname(os.path.abspath(__file__)))


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer.daemon_threads = True

if __name__ == "__main__":
    with socketserver.ThreadingTCPServer(("", PORT), Handler) as httpd:
        print("serving Hosting_Kit on :%d" % PORT)
        httpd.serve_forever()
