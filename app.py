from __future__ import annotations
import os
from dotenv import load_dotenv

# Load .env FIRST so any imported modules see the variables
ENV_PATH = "/opt/social-executor/.env"
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)
else:
    load_dotenv()

import logging
from typing import Any, Dict, List, Optional

from flask import Flask, abort, jsonify, request
import meta_client
from media_store import ingest_data_url_to_s3, ingest_inline_media
from queue_store_s3 import (
    add_draft, approve_post, get_post, mark_posted, pick_next_for_channel,
)
from scheduler import slot_channel_for_today

# Load .env when running under systemd/gunicorn as well
ENV_PATH = "/opt/social-executor/.env"
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)
else:
    load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

def _bool(v: Optional[str], default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

@app.errorhandler(Exception)
def handle_any_error(e):
    app.logger.error("Unhandled exception: %s", e, exc_info=True)
    return jsonify(error="Internal server error"), 500

@app.get("/healthz")
def healthz():
    return jsonify(ok=True)

# ---------------- Drafts ----------------
@app.post("/drafts")
def create_draft():
    # Read JSON (force=True allows unknown content-type as long as body is JSON)
    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        app.logger.warning("Error 400: Request body must be valid JSON")
        abort(400, "Request body must be valid JSON")

    caption = (payload.get("caption") or "").strip()
    if not caption:
        abort(400, "caption is required")

    # Build the draft
    draft: Dict[str, Any] = {
        "caption": caption,
        "media_url": payload.get("media_url") or None,           # optional HTTPS URL
        "media_type": (payload.get("media_type") or "image").lower(),
        "platforms": payload.get("platforms") or ["instagram", "facebook"],
        "source": payload.get("source") or "unknown",
        "notes": payload.get("notes") or "",
    }

    # Ingestion priority:
    # 1) media_inline { mime, encoding: utf8|base64, content }
    # 2) media_data_url "data:image/...;base64,..."
    # 3) media_url (already in draft)
    media_inline = payload.get("media_inline")
    media_data_url = payload.get("media_data_url")

    try:
        if isinstance(media_inline, dict) and media_inline.get("content"):
            s3_key, _mime = ingest_inline_media(
                mime=media_inline.get("mime", "image/svg+xml"),
                content=media_inline.get("content", ""),
                encoding=media_inline.get("encoding", "utf8"),
                caption_hint=caption,
            )
            draft["media_s3_key"] = s3_key
            draft["media_url"] = None
            draft["media_type"] = "image"  # we render SVG → PNG
        elif isinstance(media_data_url, str) and media_data_url.strip():
            s3_key, _mime = ingest_data_url_to_s3(media_data_url, caption_hint=caption)
            draft["media_s3_key"] = s3_key
            draft["media_url"] = None
            draft["media_type"] = "image"
        # else: media_url stays as-is or text-only if none provided
    except Exception as e:
        return jsonify(ok=False, error=f"media ingestion failed: {e}"), 400

    post_id = add_draft(draft)
    return jsonify(ok=True, post_id=post_id)

# Shared helper
def _approve_and_maybe_publish(post_id: str, publish_now: bool, channels: Optional[List[str]]):
    if not approve_post(post_id):
        abort(404, f"post_id {post_id} not found")

    published: Dict[str, Any] = {}
    if publish_now:
        post = get_post(post_id)
        if not post:
            abort(404, f"post_id {post_id} not found after approve")

        targets = channels or post.get("platforms") or ["instagram", "facebook"]
        for ch in targets:
            ch = (ch or "").lower().strip()
            try:
                if ch == "facebook":
                    res = meta_client.publish_facebook(
                        caption=post["caption"],
                        media_url=post.get("media_url"),
                        media_type=post.get("media_type") or "image",
                        media_s3_key=post.get("media_s3_key"),
                    )
                elif ch == "instagram":
                    res = meta_client.publish_instagram(
                        caption=post["caption"],
                        media_url=post.get("media_url"),
                        media_type=post.get("media_type") or "image",
                        media_s3_key=post.get("media_s3_key"),
                    )
                else:
                    continue
                mark_posted(post_id, ch)
                published[ch] = {"ok": True, "response": res}
            except Exception as e:
                published[ch] = {"ok": False, "error": str(e)}

    return {"ok": True, "post_id": post_id, "published": published}

@app.get("/approve")
def approve_link():
    post_id = request.args.get("post_id")
    if not post_id:
        abort(400, "post_id is required")
    publish_now = _bool(request.args.get("publish_now"), False)
    channels = request.args.get("channels")
    channels = [c.strip() for c in channels.split(",")] if channels else None
    result = _approve_and_maybe_publish(post_id, publish_now, channels)
    return jsonify(result)

@app.post("/approve")
def approve_api():
    data = request.get_json(force=True) or {}
    post_id = data.get("post_id")
    if not post_id:
        abort(400, "post_id is required")
    publish_now = bool(data.get("publish_now"))
    channels = data.get("channels")  # list or None
    result = _approve_and_maybe_publish(post_id, publish_now, channels)
    return jsonify(result)

# ---------------- Scheduler ----------------
@app.post("/scheduler/run")
def run_scheduler():
    slot = (request.args.get("slot") or "am").lower()
    if slot not in ("am", "pm"):
        abort(400, "slot must be am or pm")

    channel = slot_channel_for_today(slot)
    post = pick_next_for_channel(channel)
    if not post:
        return jsonify(ok=False, reason=f"No approved content for {channel}"), 200

    try:
        if channel == "facebook":
            res = meta_client.publish_facebook(
                caption=post["caption"],
                media_url=post.get("media_url"),
                media_type=post.get("media_type") or "image",
                media_s3_key=post.get("media_s3_key"),
            )
        else:
            res = meta_client.publish_instagram(
                caption=post["caption"],
                media_url=post.get("media_url"),
                media_type=post.get("media_type") or "image",
                media_s3_key=post.get("media_s3_key"),
            )
        mark_posted(post["id"], channel)
        return jsonify(ok=True, channel=channel, post_id=post["id"], response=res)
    except Exception as e:
        return jsonify(ok=False, channel=channel, post_id=post["id"], error=str(e)), 500

# ---------------- Debug (optional) ----------------
@app.get("/debug/post")
def debug_post():
    pid = request.args.get("id")
    return jsonify(get_post(pid) if pid else {})

if __name__ == "__main__":
    app.run("127.0.0.1", 8000, debug=True)

