# app.py
# Top Elite Demo Social Executor – Flask app
# Endpoints:
#   GET  /healthz
#   POST /drafts
#   POST /approve
#   GET  /approve             (email-friendly)
#   POST /scheduler/run?slot=am|pm
#
# Depends on:
#   meta_client.py       -> IG/FB Graph API helpers
#   queue_store_s3.py    -> S3-backed queue helpers
#   scheduler.py         -> AM/PM channel alternation
#
# Environment (via .env or process):
#   META_API_VERSION, IG_USER_ID, FB_PAGE_ID,
#   IG_ACCESS_TOKEN, FB_PAGE_ACCESS_TOKEN, TZ
#   QUEUE_S3_BUCKET, QUEUE_S3_KEY, RECENT_COOLDOWN_DAYS
#
# Run (local dev):
#   python app.py
# or with gunicorn in production:
#   gunicorn -w 3 -b 127.0.0.1:8000 app:app

import os
import json
import time
import logging
from typing import Dict, Any, List, Optional

from flask import Flask, request, jsonify, make_response, render_template_string
from dotenv import load_dotenv

# Load env early for local/dev
load_dotenv()

# ---- Logging ----
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("social-executor")

# ---- Local imports (your helper modules) ----
from meta_client import (
    ig_create_image_container,
    ig_create_video_container,
    ig_poll_container,
    ig_publish_from_container,
    fb_create_text_post,
    fb_create_photo_post,
    fb_create_video_post,
)
from queue_store_s3 import (
    add_draft,
    approve_post,
    pick_next_for_channel,
    mark_posted,
    _load_queue,   # private helper is fine to import in Python
)
from scheduler import slot_channel_for_today


# ---- Flask app ----
app = Flask(__name__)

# ---- Utility helpers ----

def _json_error(msg: str, code: int = 400):
    log.warning("Error %s: %s", code, msg)
    return jsonify({"error": msg}), code

def _get_post_by_id(post_id: str) -> Optional[Dict[str, Any]]:
    data = _load_queue()
    for p in data.get("posts", []):
        if p.get("id") == post_id:
            return p
    return None

def _publish_instagram(post: Dict[str, Any]) -> str:
    """
    Publish the given post to Instagram immediately.
    Returns the Instagram media ID.
    """
    caption = post.get("caption", "")
    media_url = post.get("media_url")
    media_type = (post.get("media_type") or "image").lower()

    if media_type == "image":
        creation_id = ig_create_image_container(media_url, caption)
    else:
        # video or reel
        creation_id = ig_create_video_container(
            video_url=media_url,
            caption=caption,
            reels=(media_type == "reel"),
            share_to_feed=True
        )

    ig_poll_container(creation_id, timeout_sec=600, interval_sec=5)
    media_id = ig_publish_from_container(creation_id)
    return media_id

def _publish_facebook(post: Dict[str, Any], schedule_unix: Optional[int] = None) -> str:
    """
    Publish the given post to Facebook Page (immediate or scheduled).
    Returns the FB post/video ID.
    """
    caption = post.get("caption", "")
    media_url = post.get("media_url")
    media_type = (post.get("media_type") or "image").lower()

    if media_type == "image" and media_url:
        return fb_create_photo_post(image_url=media_url, caption=caption, scheduled_unix=schedule_unix)
    elif media_type in ("video", "reel") and media_url:
        return fb_create_video_post(file_url=media_url, description=caption, scheduled_unix=schedule_unix)
    else:
        # text-only fallback
        return fb_create_text_post(message=caption, link=None, scheduled_unix=schedule_unix)

def _safe_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return default

def _parse_channels(v: Any) -> List[str]:
    if not v:
        return ["instagram", "facebook"]
    if isinstance(v, str):
        try:
            # try JSON string
            arr = json.loads(v)
            if isinstance(arr, list):
                return [str(x).lower() for x in arr]
        except Exception:
            # comma-separated
            return [s.strip().lower() for s in v.split(",") if s.strip()]
    if isinstance(v, list):
        return [str(x).lower() for x in v]
    return ["instagram", "facebook"]


# ---- Routes ----

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/")
def root():
    return {"service": "Top Elite Demo Social Executor", "ok": True}

@app.post("/drafts")
def create_draft():
    """
    Save a draft to the S3-backed queue (status=pending).
    Body JSON:
    {
      "caption": "...",                 (required)
      "media_url": "https://...",       (optional for FB text)
      "media_type": "image|video|reel|text",
      "platforms": ["instagram","facebook"],
      "source": "weekly|owner_input|manual",
      "notes": "freeform notes for context"
    }
    """
    try:
        j = request.get_json(force=True, silent=False)
    except Exception:
        return _json_error("Request body must be valid JSON", 400)

    caption = j.get("caption")
    if not caption:
        return _json_error("Missing required field: caption", 400)

    draft = {
        "caption": caption,
        "media_url": j.get("media_url"),
        "media_type": (j.get("media_type") or "image").lower(),
        "platforms": j.get("platforms") or ["instagram", "facebook"],
        "source": j.get("source", "unknown"),
        "notes": j.get("notes", "")
    }

    post_id = add_draft(draft)
    log.info("Draft created: %s", post_id)
    return jsonify({"ok": True, "post_id": post_id, "status": "pending"})

@app.post("/approve")
def approve_post_endpoint():
    """
    Owner approval (optionally publish immediately).
    Body JSON:
    {
      "post_id": "draft_123",
      "publish_now": false,
      "channels": ["instagram","facebook"],   (optional; default both)
      "schedule_unix": 0                      (optional; FB only)
    }
    """
    try:
        j = request.get_json(force=True, silent=False)
    except Exception:
        return _json_error("Request body must be valid JSON", 400)

    post_id = j.get("post_id")
    if not post_id:
        return _json_error("Missing required field: post_id", 400)

    publish_now = _safe_bool(j.get("publish_now"), False)
    channels = _parse_channels(j.get("channels"))
    schedule_unix = j.get("schedule_unix")
    try:
        schedule_unix = int(schedule_unix) if schedule_unix is not None else None
    except Exception:
        return _json_error("schedule_unix must be a UNIX integer timestamp", 400)

    if not approve_post(post_id):
        return _json_error("Post not found", 404)

    results: Dict[str, Any] = {"approved": True, "published": {}}

    if publish_now:
        post = _get_post_by_id(post_id)
        if not post:
            return _json_error("Post not found after approval", 404)

        if "instagram" in channels:
            try:
                ig_id = _publish_instagram(post)
                mark_posted(post_id, "instagram")
                results["published"]["instagram"] = ig_id
                log.info("Published to Instagram id=%s post=%s", ig_id, post_id)
            except Exception as e:
                log.exception("IG publish failed")
                results["published"]["instagram_error"] = str(e)

        if "facebook" in channels:
            try:
                fb_id = _publish_facebook(post, schedule_unix=schedule_unix)
                # Mark as posted only if not scheduled for the future
                if not schedule_unix:
                    mark_posted(post_id, "facebook")
                results["published"]["facebook"] = fb_id
                log.info("Published to Facebook id=%s post=%s scheduled=%s", fb_id, post_id, bool(schedule_unix))
            except Exception as e:
                log.exception("FB publish failed")
                results["published"]["facebook_error"] = str(e)

    return jsonify({"ok": True, "results": results})

@app.get("/approve")
def approve_get_endpoint():
    """
    Email-friendly approval link.
      /approve?post_id=...&publish_now=false
      /approve?post_id=...&publish_now=true&channels=instagram,facebook&schedule_unix=1737000000
    """
    post_id = request.args.get("post_id")
    if not post_id:
        return _json_error("Missing required query param: post_id", 400)

    publish_now = _safe_bool(request.args.get("publish_now"), False)
    channels = _parse_channels(request.args.get("channels"))
    schedule_unix = request.args.get("schedule_unix")
    try:
        schedule_unix = int(schedule_unix) if schedule_unix is not None else None
    except Exception:
        return _json_error("schedule_unix must be a UNIX integer timestamp", 400)

    if not approve_post(post_id):
        return _json_error("Post not found", 404)

    published = {}
    errors = {}

    if publish_now:
        post = _get_post_by_id(post_id)
        if not post:
            return _json_error("Post not found after approval", 404)

        if "instagram" in channels:
            try:
                ig_id = _publish_instagram(post)
                mark_posted(post_id, "instagram")
                published["instagram"] = ig_id
            except Exception as e:
                log.exception("IG publish failed (GET)")
                errors["instagram"] = str(e)

        if "facebook" in channels:
            try:
                fb_id = _publish_facebook(post, schedule_unix=schedule_unix)
                if not schedule_unix:
                    mark_posted(post_id, "facebook")
                published["facebook"] = fb_id
            except Exception as e:
                log.exception("FB publish failed (GET)")
                errors["facebook"] = str(e)

    # Simple HTML confirmation page for owner clicks
    html = """
    <!doctype html>
    <html>
      <head><meta charset="utf-8"><title>Approval Recorded</title>
      <style>
        body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; padding: 24px; }
        .ok { color: #0a7f3f; }
        .err { color: #b00020; }
        code { background:#f3f3f3; padding:2px 4px; border-radius:4px; }
      </style>
      </head>
      <body>
        <h2 class="ok">✅ Approval recorded</h2>
        <p>Post ID: <code>{{post_id}}</code></p>
        <p>Publish now: <b>{{publish_now}}</b></p>
        {% if published %}
          <h3>Published</h3>
          <pre>{{published}}</pre>
        {% endif %}
        {% if errors %}
          <h3 class="err">Errors</h3>
          <pre>{{errors}}</pre>
        {% endif %}
      </body>
    </html>
    """
    return render_template_string(
        html,
        post_id=post_id,
        publish_now=publish_now,
        published=json.dumps(published, indent=2),
        errors=json.dumps(errors, indent=2) if errors else ""
    )

@app.post("/scheduler/run")
def scheduler_run():
    """
    Twice-daily scheduler entrypoint (called by Gumloop).
    Query params:
      slot = "am" | "pm"
      channel (optional) overrides alternation
    """
    slot = (request.args.get("slot") or "am").lower()
    channel = (request.args.get("channel") or "").lower() or slot_channel_for_today(slot)
    if channel not in ("instagram", "facebook"):
        return _json_error("Channel must be 'instagram' or 'facebook'", 400)

    candidate = pick_next_for_channel(channel)
    if not candidate:
        msg = f"No approved content available for {channel}"
        log.info(msg)
        return jsonify({"ok": True, "message": msg, "channel": channel}), 200

    # Publish according to channel
    res: Dict[str, Any] = {"channel": channel, "post_id": candidate["id"]}
    try:
        if channel == "instagram":
            media_id = _publish_instagram(candidate)
            mark_posted(candidate["id"], "instagram")
            res["media_id"] = media_id
            log.info("Scheduler posted to Instagram media=%s post=%s", media_id, candidate["id"])
        else:
            media_id = _publish_facebook(candidate, schedule_unix=None)
            mark_posted(candidate["id"], "facebook")
            res["media_id"] = media_id
            log.info("Scheduler posted to Facebook media=%s post=%s", media_id, candidate["id"])
    except Exception as e:
        log.exception("Scheduler publish failed")
        return _json_error(f"Scheduler publish failed: {e}", 500)

    return jsonify({"ok": True, **res}), 200


# ---- Error handlers ----
@app.errorhandler(404)
def _404(_e):
    return _json_error("Not found", 404)

@app.errorhandler(405)
def _405(_e):
    return _json_error("Method not allowed", 405)

@app.errorhandler(500)
def _500(e):
    log.exception("Unhandled error: %s", e)
    return _json_error("Internal server error", 500)


# ---- Main ----
if __name__ == "__main__":
    # Bind to 0.0.0.0 for container/EC2; use PORT if provided (Render/Heroku style)
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

