"""The dashboard: `python -m shorts dashboard` on http://127.0.0.1:5000 (local only; on the server
Tailscale makes it reachable from your own devices).

Server-rendered Flask pages; heavy work runs as background jobs (shorts/jobs.py)."""
from __future__ import annotations

import gzip
from pathlib import Path

from flask import Flask, g, request

from ..config import Settings, all_channels
from ..jobs import Runner

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def create_app(settings: Settings, runner: Runner | None = None, start_runner: bool = True) -> Flask:
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.config["SETTINGS"] = settings
    app.extensions["runner"] = runner or Runner(settings)
    if start_runner:
        app.extensions["runner"].start()
        from ..channels import add_video_types, seed_channels
        add_video_types(settings)  # older channel files: video_type = "classic"
        seed_channels(settings)    # new channels prepared in config/channel-seeds
        from ..drive import start_checker
        start_checker(settings)    # upload via phone: spot Shorts posted from the phone every 30 min
        from .home import warm_up
        warm_up(settings, all_channels(settings.config_dir))  # YouTube's schedule ready before the first visit

    from . import bench, channels, footage_review, home, jobs, library, music, review, schedule, sources, stats, voices
    for module in (home, review, jobs, library, sources, music, voices, channels, schedule, stats, bench, footage_review):
        app.register_blueprint(module.bp)

    @app.teardown_appcontext
    def close_conn(_exc) -> None:
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    @app.after_request
    def compress(resp):
        """Pages go out gzipped (often 5-10x smaller): quicker on the phone over Tailscale."""
        if (resp.direct_passthrough or resp.status_code != 200 or "Content-Encoding" in resp.headers
                or resp.mimetype not in ("text/html", "application/json", "text/css", "text/javascript")
                or "gzip" not in request.headers.get("Accept-Encoding", "")):
            return resp
        data = resp.get_data()
        if len(data) < 1000:
            return resp
        resp.set_data(gzip.compress(data, compresslevel=5))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(resp.get_data()))
        resp.vary.add("Accept-Encoding")
        return resp

    @app.template_filter("pct")
    def pct(done, total):
        return int(100 * (done or 0) / total) if total else 0

    return app


def serve(settings: Settings, port: int = 5000) -> None:
    """Localhost only: no password page needed, nothing reachable from the network."""
    app = create_app(settings)
    print(f"Dashboard: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
