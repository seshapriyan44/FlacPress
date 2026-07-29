"""
Desktop entry point for FlacPress.

Runs the Flask app on a background thread and opens it in a native
window via pywebview instead of a browser tab. This is also the file
PyInstaller should build from — see README "Package as a desktop app".
"""

from __future__ import annotations

import threading
import webview

from app import app


def _run_flask():
    # use_reloader must be off: it forks a second process, which breaks
    # inside a PyInstaller-frozen exe.
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)


def main():
    threading.Thread(target=_run_flask, daemon=True).start()
    webview.create_window("FlacPress", "http://127.0.0.1:5000", width=1180, height=800, min_size=(820, 600))
    webview.start()


if __name__ == "__main__":
    main()
