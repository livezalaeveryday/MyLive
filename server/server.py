"""Desktop/local entry point for Live Chat Aggregator."""

from __future__ import annotations

import engineio.async_drivers.threading
import logging
import secrets
import socket
import threading
import time
from urllib.parse import quote
import webbrowser

from lca import create_app


logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("lca")


def find_free_port(start: int = 5000, host: str = "127.0.0.1") -> int:
    for port in range(start, 65536):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free TCP port is available")


def lan_address() -> str:
    """Return the address other devices on the LAN can use for this PC."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 80))
        address = probe.getsockname()[0]
        probe.close()
        if address and not address.startswith("127."):
            return address
    except OSError:
        pass
    return socket.gethostbyname(socket.gethostname())


def run() -> None:
    app = create_app()
    settings = app.extensions["lca.settings"]
    auth = app.extensions["lca.auth"]
    socketio = app.extensions["socketio"]
    # The sponsor/ad service is bundled into the same portable EXE and kept
    # on a fixed port so Cloudflare can route ads.finstall.click reliably.
    try:
        from ad_server import create_server

        ad_server = create_server()
        threading.Thread(
            target=ad_server.serve_forever,
            name="sponsor-ad-server",
            daemon=True,
        ).start()
        log.info("Sponsor ad server started on %s:8787", settings.bind_host)
    except OSError as exc:
        log.warning("Sponsor ad server unavailable on port 8787: %s", exc)
    port = find_free_port(settings.port, settings.bind_host)
    session_id = secrets.token_hex(16)
    fragment = f"token={quote(auth.token)}&session={quote(session_id)}"
    local_host = lan_address() if settings.bind_host == "0.0.0.0" else settings.bind_host
    launch_url = f"http://{local_host}:{port}/#{fragment}"

    server_thread = threading.Thread(
        target=lambda: socketio.run(
            app,
            host=settings.bind_host,
            port=port,
            allow_unsafe_werkzeug=True,
            use_reloader=False,
        ),
        name="lca-local-server",
        daemon=True,
    )
    server_thread.start()
    time.sleep(0.8)
    log.info("Dashboard available on LAN: http://%s:%d/", local_host, port)

    try:
        import webview

        webview.create_window(
            "Live Chat Aggregator",
            launch_url,
            width=1280,
            height=800,
            min_size=(900, 600),
        )
        webview.start()
    except ImportError:
        webbrowser.open(launch_url)
        log.info("Opened the local dashboard in the default browser")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        app.extensions["lca.streams"].shutdown()


if __name__ == "__main__":
    run()
