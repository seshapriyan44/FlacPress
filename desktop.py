"""
Desktop entry point for FlacPress.

Runs the Flask app on a background thread and opens it in a native
window via pywebview instead of a browser tab. This is also the file
PyInstaller builds from — see README "Building the Windows app".
"""

from __future__ import annotations

import sys
import threading

import webview

from app import app
from runtime import free_port, wait_for_server

HOST = "127.0.0.1"


def main():
    # Port 5000 is a bad thing to hardcode: it collides with other dev
    # servers and is occupied by AirPlay Receiver on macOS. Ask for it,
    # accept a neighbour if it's busy.
    port = free_port(5000, HOST)

    threading.Thread(
        target=lambda: app.run(host=HOST, port=port, debug=False,
                               use_reloader=False, threaded=True),
        daemon=True,
    ).start()

    # Open the window only once Flask is actually listening, otherwise
    # the user sees a connection error instead of the UI.
    if not wait_for_server(HOST, port):
        print("FlacPress could not start its local server.", file=sys.stderr)
        return 1

    webview.create_window(
        "FlacPress",
        f"http://{HOST}:{port}",
        width=1180,
        height=800,
        min_size=(820, 600),
    )
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
