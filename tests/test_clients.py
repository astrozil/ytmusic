import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from clients import UpstreamClients


def test_concurrent_ytmusic_calls_reuse_connections_without_pool_warnings(settings_factory, monkeypatch, caplog):
    workers = 12
    gate = threading.Barrier(workers)
    client_addresses = set()
    address_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            with address_lock:
                client_addresses.add(self.client_address)
            gate.wait(timeout=10)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    sessions = []

    class LocalYTMusic:
        def __init__(self, auth, requests_session):
            self.session = requests_session
            sessions.append(requests_session)

        def search(self, query):
            response = self.session.get(url)
            response.raise_for_status()
            return response.text

    monkeypatch.setattr("clients.YTMusic", LocalYTMusic)
    clients = UpstreamClients(settings_factory(), logging.getLogger(__name__))
    caplog.set_level(logging.WARNING, logger="urllib3.connectionpool")
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for _ in range(2):
                results = list(executor.map(
                    lambda i: clients.call_ytmusic("search", str(i), timeout=15, retries=0),
                    range(workers),
                ))
                assert results == ["ok"] * workers
        assert len(client_addresses) == workers
        assert "Connection pool is full" not in caplog.text
    finally:
        clients._ytmusic_executor.shutdown(wait=True)
        clients.http.close()
        for session in sessions:
            session.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
