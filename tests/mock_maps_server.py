"""
Test / benchmark için yerel sahte Google Maps sunucusu.

Gerçek Google'a yük bindirmeden eş zamanlılık, rate limit ve hata senaryolarını
tekrarlanabilir biçimde ölçmeyi sağlar.

URL biçimi:  http://127.0.0.1:<port>/maps/place/<ad>/data=...
  <ad> öneki davranışı belirler:
    timeout*   → sunucu yanıt vermez (istemci zaman aşımına düşer)
    drop*      → bağlantı yanıt göndermeden kapatılır (net::ERR_EMPTY_RESPONSE)
    flaky*     → ilk FLAKY_FAILS istek HTTP 503, sonra normal yanıt
    notfound*  → HTTP 404
    e500*      → HTTP 500
    diğer      → normal mekan sayfası (gecikme: latency aralığı)

Rate limit (Google benzeri): pencere içinde `rate_limit` istekten fazlası gelirse
istek /sorry/index'e yönlenir (HTTP 429, "unusual traffic") ve sunucu `penalty`
saniye boyunca TÜM istekleri engeller.
"""
from __future__ import annotations

import random
import socket
import threading
import time
import zlib
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{name} - Google Haritalar</title>
<style>body{{margin:0;font-family:sans-serif}}#pane{{position:absolute;left:0;top:0;width:400px;height:800px;
background:#fff;box-shadow:2px 0 6px #0003;padding:16px}}canvas{{position:absolute;left:400px;top:0}}</style></head>
<body><div id="pane"><h1>{name}</h1><div data-value="Yol tarifi">Yol tarifi</div></div>
<canvas id="c" width="880" height="800"></canvas>
<script>const c=document.getElementById('c').getContext('2d');
for(let i=0;i<300;i++){{c.fillStyle='hsl('+(i*37%360)+',40%,80%)';c.fillRect(Math.random()*880,Math.random()*800,60,40);}}
c.fillStyle='#d33';c.beginPath();c.arc(440,400,14,0,7);c.fill();</script></body></html>"""

SORRY = """<!doctype html><html><head><title>Sorry... unusual traffic</title></head>
<body><form action="/sorry/index" id="captcha-form">Our systems have detected unusual traffic.</form></body></html>"""


class MockMapsServer:
    FLAKY_FAILS = 1

    def __init__(self, port: int = 0, latency: tuple[float, float] = (0.2, 0.6),
                 rate_limit: int = 0, window: float = 1.0, penalty: float = 5.0, seed: int = 7) -> None:
        self.latency = latency
        self.rate_limit = rate_limit
        self.window = window
        self.penalty = penalty
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.recent: deque[float] = deque()
        self.blocked_until = 0.0
        self.flaky_seen: dict[str, int] = {}
        self.stats = {"requests": 0, "served": 0, "rate_limited": 0, "timeouts": 0, "drops": 0,
                      "not_found": 0, "e500": 0, "max_inflight": 0}
        self.inflight = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # sessiz
                pass

            def _send(self, code: int, body: str, headers: dict | None = None) -> None:
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/favicon"):
                    return self._send(404, "")
                if self.path.startswith("/sorry/"):
                    return self._send(429, SORRY)
                with server.lock:
                    server.stats["requests"] += 1
                    server.inflight += 1
                    server.stats["max_inflight"] = max(server.stats["max_inflight"], server.inflight)
                try:
                    self._handle()
                finally:
                    with server.lock:
                        server.inflight -= 1

            def _handle(self):
                path = unquote(self.path)
                name = path.split("/maps/place/", 1)[-1].split("/", 1)[0].replace("+", " ")
                now = time.monotonic()
                with server.lock:
                    limited = False
                    if server.rate_limit:
                        while server.recent and now - server.recent[0] > server.window:
                            server.recent.popleft()
                        server.recent.append(now)
                        if now < server.blocked_until or len(server.recent) > server.rate_limit:
                            limited = True
                            server.blocked_until = max(server.blocked_until, now + server.penalty)
                            server.stats["rate_limited"] += 1
                    delay = server.rng.uniform(*server.latency)
                if limited:
                    return self._send(302, "", {"Location": "/sorry/index?continue=" + self.path})
                low = name.lower()
                if low.startswith("timeout"):
                    with server.lock:
                        server.stats["timeouts"] += 1
                    time.sleep(120)
                    return
                if low.startswith("drop"):
                    with server.lock:
                        server.stats["drops"] += 1
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                if low.startswith("flaky"):
                    with server.lock:
                        n = server.flaky_seen.get(low, 0)
                        server.flaky_seen[low] = n + 1
                    if n < MockMapsServer.FLAKY_FAILS:
                        with server.lock:
                            server.stats["e500"] += 1
                        return self._send(503, "<html><title>503</title>gecici</html>")
                if low.startswith("notfound"):
                    with server.lock:
                        server.stats["not_found"] += 1
                    return self._send(404, "<html><title>404</title>yok</html>")
                if low.startswith("e500"):
                    with server.lock:
                        server.stats["e500"] += 1
                    return self._send(500, "<html><title>500</title>hata</html>")
                time.sleep(delay)
                with server.lock:
                    server.stats["served"] += 1
                return self._send(200, PAGE.format(name=name))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def url(self, name: str) -> str:
        return f"http://{self.host}/maps/place/{name.replace(' ', '+')}/data=!4m2!3m1!1s0x0:0x{zlib.crc32(name.encode()):x}"

    def __enter__(self) -> "MockMapsServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
