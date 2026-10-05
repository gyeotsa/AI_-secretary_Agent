"""One-shot, bounded OAuth callback on IPv4 loopback only; no request logging."""
from __future__ import annotations

import hmac
import os
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from core.plugin import ToolCancelledError
from core.remote_runtime import OAuthCoordinator, RemoteRuntimeError


class OAuthConsentDenied(RemoteRuntimeError):
    pass


class _LoopbackServer(HTTPServer):
    allow_reuse_address = False

    def __init__(self, *args):
        self._active_socket = None
        self._socket_lock = threading.Lock()
        super().__init__(*args)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(0.5)
        with self._socket_lock:
            self._active_socket = connection
        return connection, address

    def close_request(self, request):
        with self._socket_lock:
            if self._active_socket is request:
                self._active_socket = None
        super().close_request(request)

    def interrupt_request(self):
        with self._socket_lock:
            if self._active_socket is not None:
                try:
                    self._active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def handle_error(self, request, client_address):
        # Never let HTTPServer print callback codes, state, or provider text.
        pass


class OAuthLoopbackConnection:
    CALLBACK_PATH = "/oauth/callback"

    def __init__(self, oauth: OAuthCoordinator, *, browser_open=None, timeout=180):
        if not 1 <= timeout <= 600:
            raise ValueError("OAuth timeout must be between 1 and 600 seconds")
        self.oauth = oauth
        self.browser_open = browser_open or webbrowser.open
        self.timeout = timeout
        self._cancelled = threading.Event()
        self._lock = threading.RLock()
        self._state = ""
        self._server = None
        self._deadline = None
        self.redirect_uri = ""

    def cancel(self):
        with self._lock:
            self._cancelled.set()
            if self._state:
                self.oauth.cancel(self._state)
            if self._server is not None:
                self._server.interrupt_request()

    def checkpoint(self):
        if self._cancelled.is_set():
            raise ToolCancelledError("OAuth 연결이 취소되었습니다.")
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise RemoteRuntimeError("OAuth 연결 시간이 만료되었습니다.")

    @staticmethod
    def default_port(provider):
        # Microsoft ignores dynamic ports for localhost, not the IP literal.
        # Keep the listener on 127.0.0.1 and let users register this exact URI.
        value = os.getenv("MICROSOFT_OAUTH_LOOPBACK_PORT", "8765") if provider == "microsoft" else "0"
        try:
            port = int(value)
        except (TypeError, ValueError):
            raise RemoteRuntimeError("OAuth 콜백 포트 설정이 올바르지 않습니다.") from None
        if not 0 <= port <= 65535 or (provider == "microsoft" and port == 0):
            raise RemoteRuntimeError("OAuth 콜백 포트 설정이 올바르지 않습니다.")
        return port

    def connect(self, provider, account, *, client_id=None, client_secret=None, port=None,
                on_browser=None):
        self.checkpoint()
        if provider not in self.oauth.SPECS:
            raise RemoteRuntimeError("지원하지 않는 OAuth 제공자입니다.")
        port = self.default_port(provider) if port is None else port
        if type(port) is not int or not 0 <= port <= 65535 or (provider == "microsoft" and not port):
            raise RemoteRuntimeError("OAuth 콜백 포트 설정이 올바르지 않습니다.")
        deadline = time.monotonic() + self.timeout
        self._deadline = deadline
        outcome = {}
        connection = self

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send_error(self, code, message=None, explain=None):
                self._reply(code, "Invalid OAuth callback.")

            def _reply(self, status, text):
                body = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def do_GET(self):
                if (len(self.path) > 16384 or self.client_address[0] != "127.0.0.1"
                        or self.headers.get_all("Host", []) != [urlsplit(connection.redirect_uri).netloc]
                        or self.headers.get_all("Origin", [])):
                    self._reply(400, "Invalid OAuth callback.")
                    return
                try:
                    parsed = urlsplit(self.path)
                    params = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True,
                                      max_num_fields=20)
                    valid = (not parsed.scheme and not parsed.netloc and not parsed.fragment
                             and parsed.path == connection.CALLBACK_PATH
                             and len(params.get("state", [])) == 1
                             and hmac.compare_digest(params["state"][0], connection._state)
                             and all(len(values) == 1 for values in params.values())
                             and (bool(params.get("code", [""])[0]) != bool(params.get("error", [""])[0])))
                except (ValueError, TypeError, UnicodeError):
                    valid = False
                if not valid or outcome or connection._cancelled.is_set() or time.monotonic() >= deadline:
                    self._reply(400, "Invalid or expired OAuth callback.")
                    return
                if "error" in params:
                    outcome["denied"] = True  # Do not retain/echo provider error descriptions.
                else:
                    outcome["code"] = params["code"][0]
                self._reply(200, "OAuth response received. Return to ANIS to check the result.")

        server = _LoopbackServer(("127.0.0.1", port), Callback)
        server.timeout = 0.2
        self._server = server
        expiry = threading.Timer(self.timeout, server.interrupt_request)
        expiry.daemon = True
        expiry.start()
        try:
            self.redirect_uri = f"http://127.0.0.1:{server.server_port}{self.CALLBACK_PATH}"
            with self._lock:
                self.checkpoint()
                begun = self.oauth.begin(provider, account, self.redirect_uri,
                                         client_id=client_id, client_secret=client_secret,
                                         expected_identity=account)
                self._state = begun["state"]
            client_id = client_secret = None
            self.checkpoint()
            if on_browser is not None:
                on_browser(self.redirect_uri)
            if not self.browser_open(begun["authorization_url"]):
                raise RemoteRuntimeError("기본 브라우저를 열지 못했습니다.")
            while not outcome:
                self.checkpoint()
                if time.monotonic() >= deadline:
                    raise RemoteRuntimeError("OAuth 연결 시간이 만료되었습니다.")
                server.handle_request()
            self.checkpoint()
            if outcome.get("denied"):
                raise OAuthConsentDenied("OAuth 동의를 거부했습니다. 저장하지 않았습니다.")
            # Callback receipt is not token/identity verification; no browser
            # success page claims that the account was connected.
            server.server_close()
            return self.oauth.complete(self._state, outcome["code"], check_cancelled=self.checkpoint)
        finally:
            expiry.cancel()
            server.server_close()
            with self._lock:
                if self._state:
                    self.oauth.cancel(self._state)
                self._state = ""
                self._server = None
            outcome.clear()
