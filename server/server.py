"""Desktop/local entry point for Live Chat Aggregator."""

from __future__ import annotations

import engineio.async_drivers.threading
import logging
import socket
import threading
import time
from urllib.parse import quote
import webbrowser

from lca import create_app
from lca.desktop_identity import load_desktop_identity, DesktopIdentityError


logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("lca")


def require_available_port(port: int, host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        try:
            candidate.bind((host, port))
        except OSError as exc:
            raise RuntimeError(f"MyLive port {port} is occupied or unavailable. Close the other app/instance or explicitly set LCA_PORT; the port was not changed.") from exc
    return port


def desktop_launch_url(token: str, session_id: str, port: int, bind_host: str = "0.0.0.0") -> str:
    host = "127.0.0.1" if bind_host in {"0.0.0.0", "127.0.0.1", "localhost"} else bind_host
    return f"http://{host}:{port}/#token={quote(token)}&session={quote(session_id)}"


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
    identity = load_desktop_identity()
    app = create_app({"LOCAL_AUTH_TOKEN": identity["token"]})
    settings = app.extensions["lca.settings"]
    auth = app.extensions["lca.auth"]
    socketio = app.extensions["socketio"]
    port = require_available_port(settings.port, settings.bind_host)
    launch_url = desktop_launch_url(auth.token, identity["session"], port, settings.bind_host)
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
    log.info("Dashboard available locally: http://127.0.0.1:%d/", port)

    try:
        import webview

        if __import__('os').name == 'nt':
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('MyLive.Desktop')
            except Exception:
                pass
        window = webview.create_window(
            "MyLive",
            launch_url,
            width=1280,
            height=800,
            min_size=(900, 600),
        )
        def apply_window_icon():
            try:
                import ctypes
                from pathlib import Path
                if __import__('os').name != 'nt':
                    return
                user32 = ctypes.windll.user32
                user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
                user32.LoadImageW.restype = ctypes.c_void_p
                user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
                user32.SendMessageW.restype = ctypes.c_ssize_t
                handle = int(window.native.Handle.ToInt64())
                icon_path = str(Path(__file__).resolve().parent / 'assets' / 'mylive-icon.ico')
                apply_window_icon.handles = []
                for kind, size in ((0, 32), (1, 64)):
                    icon = user32.LoadImageW(None, icon_path, 1, size, size, 0x10)
                    if icon:
                        apply_window_icon.handles.append(icon)
                        user32.SendMessageW(handle, 0x80, kind, icon)
            except Exception:
                pass
        window.events.shown += apply_window_icon
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
    try:
        run()
    except Exception as exc:
        # Visible in the windowed EXE too. Never include credential-bearing URLs.
        detail = str(exc) if isinstance(exc, DesktopIdentityError) or str(exc).startswith("MyLive port ") else "Check the desktop profile, bind host and port configuration."
        message = "MyLive could not start. " + detail
        if __import__('os').name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "MyLive startup error", 0x10)
        else:
            log.error("%s", message)
        raise SystemExit(1)
