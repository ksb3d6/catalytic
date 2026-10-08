#!/usr/bin/env python3
"""Railway static server for the hosted (SCRUBBED) AI Build-Out app.

Railway's nixpacks detects it and runs the kit at a public URL
(`railway up --service aibo-app` from this folder).
No-store headers so a fresh push goes live immediately; the PWA service worker
(sw.js v3) is NETWORK FIRST over the kit's own files only — its cache is the
offline / slow-network fallback, and it never caches the backend's answers.
"""
import http.server
import os
import socketserver

PORT = int(os.environ.get("PORT", 8080))
os.chdir(os.path.dirname(os.path.abspath(__file__)))


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        # 10-07 sweep: no other site may frame the app — a framing page could overlay its own UI on the
        # bearer-backed one-tap controls (Delete list, Sign out everywhere). Same-origin frames (yields.html) still load.
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer.daemon_threads = True

if __name__ == "__main__":
    with socketserver.ThreadingTCPServer(("", PORT), Handler) as httpd:
        print("serving Hosting_Kit on :%d" % PORT)
        httpd.serve_forever()
