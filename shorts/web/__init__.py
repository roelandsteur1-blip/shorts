"""The dashboard: `python -m shorts dashboard` on http://127.0.0.1:5000 (local only; on the server
Tailscale makes it reachable from your own devices).

Server-rendered Flask pages; heavy work runs as background jobs (shorts/jobs.py)."""
from __future__ import annotations

from pathlib import Path

from flask import Flask, g

from ..config import Settings
from ..jobs import Runner

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def create_app(settings: Settings, runner: Runner | None = None, start_runner: bool = True) -> Flask:
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.config["SETTINGS"] = settings
    app.extensions["runner"] = runner or Runner(settings)
    if start_runner:
        app.extensions["runner"].start()

    from . import channels, home, jobs, library, music, review, schedule, sources, stats, voices
    for module in (home, review, jobs, library, sources, music, voices, channels, schedule, stats):
        app.register_blueprint(module.bp)

    @app.teardown_appcontext
    def close_conn(_exc) -> None:
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    @app.context_processor
    def speed_mode() -> dict:
        from ..speed import get_mode, on_battery
        return {"speed_mode": get_mode(settings.data_dir), "on_battery": on_battery()}

    @app.template_filter("pct")
    def pct(done, total):
        return int(100 * (done or 0) / total) if total else 0

    return app


def serve(settings: Settings, port: int = 5000) -> None:
    """Localhost only: no password page needed, nothing reachable from the network."""
    app = create_app(settings)
    print(f"Dashboard: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
