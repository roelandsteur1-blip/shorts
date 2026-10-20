"""Add-channel wizard and channel settings: the form writes config/channels/<id>.toml."""
from __future__ import annotations

import json

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for

from .. import categorize, channels as chans, formats as fmtlib, inspiration, llm, looks, sources, tags as taglayers
from ..config import all_channels, load_channel
from ..music import MUSIC_EXT
from ..upload import found_path, token_path
from ..voice import SPEEDS, VOICES, label
from .common import channel_or_404, conn, runner, settings
from .jobs import start_job

bp = Blueprint("channels", __name__)


def _lines(text: str | None) -> list[str]:
    return [x.strip() for x in (text or "").replace(",", "\n").splitlines() if x.strip()]


def form_to_data(form, base: dict) -> dict:
    data = dict(base)
    for key in ("name", "niche", "hero_query"):
        if key in form:
            data[key] = form.get(key, "").strip() or None
    if "id" in form:
        data["id"] = form["id"].strip().lower()
    data["language"] = form.get("language", data.get("language", "en"))
    if "niche_tags" in form:
        data["niche_tags"] = taglayers.pick(_lines(form["niche_tags"]))
    data["search_terms"] = _lines(form.get("search_terms"))
    if "voice" in form:
        data["voice"] = form["voice"].strip() or None
    if form.get("voice_speed"):
        try:
            data["voice_speed"] = float(form["voice_speed"])
        except ValueError:
            data["voice_speed"] = form["voice_speed"]
    if "music_terms" in form:
        data["music_terms"] = [t for t in (x.strip() for x in form["music_terms"].splitlines()) if t]
    data["categories"] = form.getlist("categories")
    data["sources"] = form.getlist("sources")
    data["publish_times_utc"] = sorted(_lines(form.get("publish_times_utc")))
    for key in ("min_seconds", "max_seconds", "uploads_per_day"):
        try:
            data[key] = int(form.get(key, data.get(key)))
        except (TypeError, ValueError):
            data[key] = form.get(key)
    data["auto_run"] = form.get("auto_run") == "1"
    if "look" in form:
        data["style"] = dict(data.get("style") or chans.DEFAULT_STYLE)
        if form["look"]:
            data["style"]["look"] = form["look"]
        else:
            data["style"].pop("look", None)
    if "title_bar" in form:
        data["style"] = dict(data.get("style") or chans.DEFAULT_STYLE)
        if form["title_bar"]:
            data["style"]["title_bar"] = form["title_bar"]
        else:
            data["style"].pop("title_bar", None)
    if "inspiration" in form:
        data["inspiration"] = [x.strip() for x in form["inspiration"].splitlines() if x.strip()]
    if "format_count" in form:
        data["library_formats"] = form.get("library_formats") == "1"
    formats = []
    for i in range(int(form.get("format_count", 0))):
        fid = (form.get(f"format_id_{i}") or "").strip()
        if not fid:
            continue
        formats.append({"id": fid, "title": (form.get(f"format_title_{i}") or "").strip(),
                        "brief": (form.get(f"format_brief_{i}") or "").replace("\r\n", "\n").strip(),
                        "enabled": form.get(f"format_enabled_{i}") == "1"})
    data["formats"] = formats
    return data


def _context(data: dict, errors=None, new=False) -> dict:
    cats = categorize.load_categories(settings())
    counts = dict(conn().execute(
        """SELECT c.category, COUNT(*) FROM scene_categories c JOIN scenes sc ON sc.id = c.scene_id
            WHERE c.confidence > 0 AND sc.rejected = 0 GROUP BY c.category""").fetchall())
    # Other channels' looks and voices: each channel should look and sound like itself.
    others = {c.id: c for c in all_channels(settings().config_dir) if c.id != data.get("id")}
    same_voice = [c.name for c in others.values() if data.get("voice") and c.voice == data.get("voice")]
    same_look = [c.name for c in others.values() if (data.get("style") or {}).get("look")
                 and c.style.get("look") == (data.get("style") or {}).get("look")]
    lib = None
    if data.get("id") and not new:
        try:
            lib = [f for f in fmtlib.for_channel(load_channel(data["id"], settings().config_dir))
                   if f["id"] not in {x.get("id") for x in data.get("formats") or []}]
        except Exception:  # noqa: BLE001
            lib = None
    return {"d": data, "errors": errors or [], "new": new, "cats": cats, "counts": counts,
            "sources": sources.all_sources(), "languages": chans.LANGUAGES, "voices": VOICES, "speeds": SPEEDS, "voice_label": label,
            "looks": looks.LABELS, "title_bars": looks.TITLE_BARS, "same_voice": same_voice, "same_look": same_look, "library_extra": lib,
            "niche_default": taglayers.NICHE_DEFAULTS.get(data.get("id") or "", [])}


@bp.get("/channels/new")
def new_start():
    return render_template("channel_new.html")


@bp.get("/channels/new/describe")
def new_describe():
    return render_template("channel_form.html", **_context(chans.new_channel("", "", "en", ""), new=True))


@bp.post("/channels/new")
def new_save():
    data = form_to_data(request.form, chans.new_channel("", "", "en", ""))
    known = {c.id for c in categorize.load_categories(settings())}
    errors = chans.save(settings(), data, known, new=True)
    if errors:
        return render_template("channel_form.html", **_context(data, errors, new=True)), 400
    return redirect(url_for("channels.page", cid=data["id"], created=1))


@bp.post("/channels/draft")
def draft():
    f = request.form
    if not (f.get("name") and f.get("niche")):
        return jsonify(error="fill in name and niche first"), 400
    cats = {c.id: c.label for c in categorize.load_categories(settings())}
    try:
        return jsonify(chans.draft(settings(), f["name"], f.get("language", "en"), f["niche"], cats))
    except llm.LLMError as e:
        return jsonify(error=str(e)), 502


@bp.get("/channels/<cid>")
def page(cid: str):
    ch = channel_or_404(cid)
    data = chans.raw(settings(), cid)
    music = settings().music_dir
    tracks = sum(1 for p in music.glob("*") if p.suffix.lower() in MUSIC_EXT) if music.exists() else 0
    fp = found_path(settings(), cid)
    found = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else None
    connect_job = conn().execute("""SELECT * FROM jobs WHERE channel=? AND command LIKE '["connect"%'
                                    ORDER BY id DESC LIMIT 1""", (cid,)).fetchone()
    research_job = conn().execute("""SELECT * FROM jobs WHERE channel=? AND command LIKE '["research"%'
                                     ORDER BY id DESC LIMIT 1""", (cid,)).fetchone()
    return render_template("channel_form.html", **_context(data), ch=ch, music=music, tracks=tracks,
                           bank=inspiration.bank(conn(), cid), research_job=research_job,
                           connected=token_path(settings(), cid).exists(),
                           found=found, connect_job=connect_job, created=request.args.get("created"),
                           saved=request.args.get("saved"))


@bp.post("/channels/<cid>")
def save(cid: str):
    channel_or_404(cid)
    data = form_to_data(request.form, chans.raw(settings(), cid))
    data["id"] = cid  # the id is the file name; it can't change
    known = {c.id for c in categorize.load_categories(settings())}
    errors = chans.save(settings(), data, known)
    if errors:
        return render_template("channel_form.html", **_context(data, errors), ch=channel_or_404(cid)), 400
    return redirect(url_for("channels.page", cid=cid, saved=1))


@bp.post("/channels/<cid>/connect")
def connect(cid: str):
    channel_or_404(cid)
    # A login still waiting for Google (any channel) holds the server's login port: stop it first.
    for (jid,) in conn().execute("""SELECT id FROM jobs WHERE command LIKE '["connect"%'
                                     AND status IN ('queued', 'running')""").fetchall():
        runner().stop(conn(), jid)
    args = ["connect"] + (["--fresh"] if request.form.get("fresh") else [])
    return start_job(cid, args, "Connect YouTube")


@bp.post("/channels/<cid>/confirm")
def confirm(cid: str):
    channel_or_404(cid)
    fp = found_path(settings(), cid)
    if not fp.exists():
        abort(400)
    found = json.loads(fp.read_text(encoding="utf-8"))
    data = chans.raw(settings(), cid)
    data["youtube_channel_id"] = found["id"]
    known = {c.id for c in categorize.load_categories(settings())}
    errors = chans.save(settings(), data, known)
    if errors:
        abort(400, "; ".join(errors))
    return redirect(url_for("channels.page", cid=cid, saved=1))


@bp.post("/channels/<cid>/research")
def research(cid: str):
    channel_or_404(cid)
    return start_job(cid, ["research"], "Competitor research")


@bp.post("/channels/<cid>/suggest")
def suggest(cid: str):
    """Channels with the most-viewed Shorts for the niche, to pick inspiration channels from."""
    ch = channel_or_404(cid)
    try:
        return jsonify(channels=inspiration.suggest_channels(inspiration.reader(settings(), ch), ch))
    except Exception as e:  # noqa: BLE001 - no login, quota, offline: say so on the page
        return jsonify(error=str(e)), 502


@bp.post("/channels/<cid>/try")
def try_it(cid: str):
    channel_or_404(cid)
    return start_job(cid, ["make", "-n", "3"], "Try it: make 3 Shorts")
