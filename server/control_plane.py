"""Loopback-only entrypoint for the Wave 2 Home Server control plane.

Runtime readiness never authorizes an external Tunnel or beta launch.
"""

from __future__ import annotations

import logging
from lca import create_app


logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
app = create_app({"RUNTIME_ROLE": "control-plane"})


if __name__ == "__main__":
    settings = app.extensions["lca.settings"]
    logging.getLogger("lca.control-plane").warning(
        "Starting offline control plane; external launch remains NO-GO"
    )
    app.run(host=settings.bind_host, port=settings.port, debug=False, use_reloader=False)
