"""
S3-backed queue for approved social posts.

Schema (JSON in S3):
{
  "posts": [
    {
      "id": "draft_1737060123456",
      "status": "pending" | "approved",
      "created_at": "2025-09-17T23:01:02.123456+00:00",
      "caption": "...",
      "media_url": "https://...",
      "media_type": "image" | "video" | "reel" | "text",
      "platforms": ["instagram","facebook"],
      "source": "weekly|owner_input|manual|unknown",
      "notes": "freeform",
      "last_posted_at": { "instagram": "ISO-8601", "facebook": "ISO-8601" },
      "history": [
        {"ts":"ISO-8601","event":"approved"},
        {"ts":"ISO-8601","event":"posted:instagram"}
      ]
    }
  ]
}
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

# ---- Configuration via env ----
BUCKET = os.getenv("QUEUE_S3_BUCKET") or ""
KEY = os.getenv("QUEUE_S3_KEY") or "social/approved_posts.json"
COOLDOWN_DAYS = int(os.getenv("RECENT_COOLDOWN_DAYS", "3"))

VALID_MEDIA_TYPES = {"image", "video", "reel", "text"}
VALID_PLATFORMS = {"instagram", "facebook"}

s3 = boto3.client("s3")


# ---- Time helpers ----
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---- S3 helpers ----
def _empty_doc() -> Dict[str, Any]:
    return {"posts": []}


def _ensure_bucket_key_exists() -> None:
    """
    If the JSON file doesn't exist, create it with an empty document.
    """
    try:
        s3.head_object(Bucket=BUCKET, Key=KEY)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            s3.put_object(
                Bucket=BUCKET,
                Key=KEY,
                Body=json.dumps(_empty_doc(), indent=2).encode("utf-8"),
                ContentType="application/json",
            )
        else:
            raise


def _load_queue() -> Dict[str, Any]:
    """
    Load the queue JSON from S3. (Imported by app.py as well.)
    """
    if not BUCKET:
        raise RuntimeError("QUEUE_S3_BUCKET environment variable is not set")
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=KEY)
        raw = obj["Body"].read()
        return json.loads(raw.decode("utf-8"))
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound"):
            _ensure_bucket_key_exists()
            return _empty_doc()
        raise


def _save_queue(data: Dict[str, Any]) -> None:
    s3.put_object(
        Bucket=BUCKET,
        Key=KEY,
        Body=json.dumps(data, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


# ---- CRUD operations ----
def _normalize_draft(d: Dict[str, Any]) -> Dict[str, Any]:
    # status
    d.setdefault("status", "pending")

    # media_type
    mt = (d.get("media_type") or "image").lower().strip()
    if mt not in VALID_MEDIA_TYPES:
        mt = "image"
    d["media_type"] = mt

    # platforms
    plats = d.get("platforms") or ["instagram", "facebook"]
    if isinstance(plats, (list, tuple)):
        plats = [str(x).lower().strip() for x in plats if str(x).lower().strip() in VALID_PLATFORMS]
    else:
        plats = ["instagram", "facebook"]
    d["platforms"] = plats or ["instagram", "facebook"]

    d.setdefault("source", "unknown")
    d.setdefault("notes", "")
    d.setdefault("last_posted_at", {})
    d.setdefault("history", [])
    return d


def add_draft(draft: Dict[str, Any]) -> str:
    """
    Store a new draft with status=pending.
    Returns the generated post ID.
    """
    data = _load_queue()
    draft = _normalize_draft(draft)
    draft_id = draft.get("id") or f"draft_{int(time.time()*1000)}"
    draft["id"] = draft_id
    draft["created_at"] = _iso(_now_utc())

    data.setdefault("posts", [])
    data["posts"].append(draft)
    _save_queue(data)
    return draft_id


def approve_post(post_id: str) -> bool:
    """
    Mark a draft as approved. Returns True if updated.
    """
    data = _load_queue()
    for p in data.get("posts", []):
        if p.get("id") == post_id:
            p["status"] = "approved"
            p.setdefault("last_posted_at", {})
            p.setdefault("history", [])
            p["history"].append({"ts": _iso(_now_utc()), "event": "approved"})
            _save_queue(data)
            return True
    return False


def mark_posted(post_id: str, channel: str) -> bool:
    """
    Mark an approved item as posted on a given channel ("instagram"|"facebook").
    """
    channel = channel.lower()
    if channel not in VALID_PLATFORMS:
        return False

    data = _load_queue()
    for p in data.get("posts", []):
        if p.get("id") == post_id:
            p.setdefault("last_posted_at", {})
            p["last_posted_at"][channel] = _iso(_now_utc())
            p.setdefault("history", [])
            p["history"].append({"ts": _iso(_now_utc()), "event": f"posted:{channel}"})
            _save_queue(data)
            return True
    return False


# ---- Selection logic for scheduler ----
def _cooldown_ok(post: Dict[str, Any], channel: str, days: int = COOLDOWN_DAYS) -> bool:
    """
    Enforce a minimum gap between re-using the same asset on a channel.
    """
    lp = (post.get("last_posted_at") or {}).get(channel)
    if not lp:
        return True
    try:
        last = datetime.fromisoformat(lp)
    except Exception:
        return True
    return (_now_utc() - last) >= timedelta(days=days)


def pick_next_for_channel(channel: str) -> Optional[Dict[str, Any]]:
    """
    Choose the next approved post to publish for the given channel.
    Strategy: among approved posts whose 'platforms' include channel and are
    outside cooldown, pick the one with the oldest last-posted-on-channel
    (or oldest created_at if never posted).
    """
    channel = channel.lower().strip()
    data = _load_queue()
    approved: List[Dict[str, Any]] = [
        p for p in data.get("posts", [])
        if p.get("status") == "approved" and channel in (p.get("platforms") or [])
    ]

    def sort_key(p: Dict[str, Any]):
        last = (p.get("last_posted_at") or {}).get(channel)
        created = p.get("created_at") or "1970-01-01T00:00:00+00:00"
        # Sort by last posted (None first), then by created_at
        return (last or "1970-01-01T00:00:00+00:00", created)

    approved.sort(key=sort_key)

    for p in approved:
        if _cooldown_ok(p, channel, COOLDOWN_DAYS):
            return p
    return None


# ---- Optional admin helpers (handy for debugging) ----
def list_posts(status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    data = _load_queue()
    posts = data.get("posts", [])
    if status:
        posts = [p for p in posts if p.get("status") == status]
    return posts[:limit]


def get_post(post_id: str) -> Optional[Dict[str, Any]]:
    data = _load_queue()
    for p in data.get("posts", []):
        if p.get("id") == post_id:
            return p
    return None


def delete_post(post_id: str) -> bool:
    data = _load_queue()
    before = len(data.get("posts", []))
    data["posts"] = [p for p in data.get("posts", []) if p.get("id") != post_id]
    after = len(data["posts"])
    if after != before:
        _save_queue(data)
        return True
    return False

