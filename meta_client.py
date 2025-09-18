# meta_client.py
# Helpers for Instagram Graph API + Facebook Pages API
# Functions expected by app.py:
#   ig_create_image_container(image_url, caption) -> creation_id
#   ig_create_video_container(video_url, caption, reels=False, share_to_feed=None, cover_url=None) -> creation_id
#   ig_poll_container(creation_id, timeout_sec=300, interval_sec=5) -> None
#   ig_publish_from_container(creation_id) -> media_id
#   fb_create_text_post(message, link=None, scheduled_unix=None) -> post_id
#   fb_create_photo_post(image_url, caption=None, scheduled_unix=None) -> post_id
#   fb_create_video_post(file_url, description=None, scheduled_unix=None) -> video_id
#
# Environment variables (load before use or via python-dotenv in app.py):
#   META_API_VERSION         (default "v23.0")
#   IG_USER_ID               (Instagram Business/Creator account ID)
#   FB_PAGE_ID               (Facebook Page ID)
#   IG_ACCESS_TOKEN          (long-lived user token w/ instagram_basic + instagram_content_publish)
#   FB_PAGE_ACCESS_TOKEN     (Page Access Token w/ pages_manage_posts)
#
# Notes:
# - All requests use a short timeout and minimal retry/backoff.
# - For FB scheduled posts, scheduled_unix is a UNIX timestamp (seconds).
# - Instagram containers expire (~24h); only create after human approval.

import os
import time
import logging
from typing import Optional, Dict, Any, Tuple

import requests

LOG = logging.getLogger("meta-client")
LOG.setLevel(logging.INFO)

API_VER = os.getenv("META_API_VERSION", "v23.0").strip()
IG_USER_ID = os.getenv("IG_USER_ID", "").strip()
FB_PAGE_ID = os.getenv("FB_PAGE_ID", "").strip()
IG_TOKEN = os.getenv("IG_ACCESS_TOKEN", "").strip()
FB_PAGE_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN", "").strip()

GRAPH = f"https://graph.facebook.com/{API_VER}"

DEFAULT_TIMEOUT = float(os.getenv("HTTP_TIMEOUT_S", "20"))
RETRY_COUNT = int(os.getenv("HTTP_RETRIES", "3"))
RETRY_BACKOFF_S = float(os.getenv("HTTP_RETRY_BACKOFF_S", "1.5"))


class MetaError(RuntimeError):
    """Raised when the Meta Graph API returns an error."""


def _check_env() -> None:
    missing = []
    if not IG_USER_ID:
        missing.append("IG_USER_ID")
    if not FB_PAGE_ID:
        missing.append("FB_PAGE_ID")
    if not IG_TOKEN:
        missing.append("IG_ACCESS_TOKEN")
    if not FB_PAGE_TOKEN:
        missing.append("FB_PAGE_ACCESS_TOKEN")
    if missing:
        raise MetaError(f"Missing required environment variables: {', '.join(missing)}")


def _request(method: str, url: str, *, params: Dict[str, Any] = None,
             data: Dict[str, Any] = None, files: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Small wrapper around requests with simple retries/backoff.
    Sends form-encoded data (Graph API prefers form fields).
    """
    params = params or {}
    data = data or {}

    last_err: Optional[Exception] = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = requests.request(
                method.upper(),
                url,
                params=params,
                data=data,
                files=files,
                timeout=DEFAULT_TIMEOUT,
            )
            if resp.ok:
                return resp.json()
            # Try to extract Graph error for clarity
            try:
                j = resp.json()
            except Exception:
                j = {}
            err = j.get("error") or {}
            message = err.get("message") or resp.text
            code = err.get("code")
            type_ = err.get("type")
            fbtrace = err.get("fbtrace_id")
            LOG.warning("Graph API error %s %s: %s (code=%s type=%s fbtrace=%s)",
                        method.upper(), url, message, code, type_, fbtrace)
            # Non-200: raise to trigger retry or bubble up
            last_err = MetaError(f"{resp.status_code}: {message}")
        except requests.RequestException as e:
            last_err = e
            LOG.warning("HTTP error (%s %s) attempt %d/%d: %s",
                        method.upper(), url, attempt, RETRY_COUNT, e)

        if attempt < RETRY_COUNT:
            time.sleep(RETRY_BACKOFF_S * attempt)

    assert last_err is not None
    raise MetaError(f"Request failed after {RETRY_COUNT} attempts: {last_err}")


# ----------------------------
# Instagram: create container → poll → publish
# ----------------------------

def ig_create_image_container(image_url: str, caption: str) -> str:
    """Create an Instagram media container for an IMAGE post. Returns creation_id."""
    _check_env()
    if not image_url:
        raise MetaError("image_url is required for IG image container")
    url = f"{GRAPH}/{IG_USER_ID}/media"
    data = {
        "image_url": image_url,
        "caption": caption or "",
        "access_token": IG_TOKEN,
    }
    j = _request("POST", url, data=data)
    creation_id = j.get("id")
    if not creation_id:
        raise MetaError(f"Missing creation_id in response: {j}")
    LOG.info("IG image container created: %s", creation_id)
    return creation_id


def ig_create_video_container(
    video_url: str,
    caption: str,
    reels: bool = False,
    share_to_feed: Optional[bool] = None,
    cover_url: Optional[str] = None
) -> str:
    """
    Create an Instagram media container for VIDEO or REELS. Returns creation_id.
    Some accounts/apps can set media_type=REELS; otherwise treat as video.
    """
    _check_env()
    if not video_url:
        raise MetaError("video_url is required for IG video/reel container")
    url = f"{GRAPH}/{IG_USER_ID}/media"
    data = {
        "video_url": video_url,
        "caption": caption or "",
        "access_token": IG_TOKEN,
    }
    if cover_url:
        data["cover_url"] = cover_url
    if reels:
        # When supported, this hints Reels; otherwise Meta may infer by aspect/duration.
        data["media_type"] = "REELS"
        if share_to_feed is not None:
            data["share_to_feed"] = "true" if share_to_feed else "false"

    j = _request("POST", url, data=data)
    creation_id = j.get("id")
    if not creation_id:
        raise MetaError(f"Missing creation_id in response: {j}")
    LOG.info("IG video/reel container created: %s", creation_id)
    return creation_id


def ig_poll_container(creation_id: str, timeout_sec: int = 300, interval_sec: int = 5) -> None:
    """
    Poll the container until status_code == FINISHED or timeout.
    Containers expire (~24h); create just-in-time after approval.
    """
    _check_env()
    url = f"{GRAPH}/{creation_id}"
    params = {
        "fields": "status_code,status",
        "access_token": IG_TOKEN,
    }
    waited = 0
    while waited <= timeout_sec:
        j = _request("GET", url, params=params)
        status_code = j.get("status_code") or j.get("status")
        if str(status_code).upper() == "FINISHED":
            LOG.info("IG container ready: %s", creation_id)
            return
        time.sleep(interval_sec)
        waited += interval_sec
    raise MetaError(f"IG container not ready before timeout ({timeout_sec}s): {creation_id}")


def ig_publish_from_container(creation_id: str) -> str:
    """Publish a prepared Instagram container. Returns IG media_id."""
    _check_env()
    url = f"{GRAPH}/{IG_USER_ID}/media_publish"
    data = {
        "creation_id": creation_id,
        "access_token": IG_TOKEN,
    }
    j = _request("POST", url, data=data)
    media_id = j.get("id")
    if not media_id:
        raise MetaError(f"Missing media id in response: {j}")
    LOG.info("IG published media_id=%s from creation_id=%s", media_id, creation_id)
    return media_id


# ----------------------------
# Facebook Page: feed / photos / videos
# ----------------------------

def _apply_fb_schedule(data: Dict[str, Any], scheduled_unix: Optional[int]) -> None:
    """Attach scheduling fields for FB Page posts (if provided)."""
    if scheduled_unix is not None:
        data["published"] = "false"
        data["scheduled_publish_time"] = str(int(scheduled_unix))


def fb_create_text_post(message: str, link: Optional[str] = None,
                        scheduled_unix: Optional[int] = None) -> str:
    """
    Create a text (optionally link) post on the Page feed. Returns post_id.
    If scheduled_unix is provided, the post will be scheduled (published=false).
    """
    _check_env()
    if not message:
        raise MetaError("message is required for FB text post")

    url = f"{GRAPH}/{FB_PAGE_ID}/feed"
    data = {
        "message": message,
        "access_token": FB_PAGE_TOKEN,
    }
    if link:
        data["link"] = link
    _apply_fb_schedule(data, scheduled_unix)

    j = _request("POST", url, data=data)
    post_id = j.get("id")
    if not post_id:
        raise MetaError(f"Missing post id in response: {j}")
    LOG.info("FB text/link post id=%s (scheduled=%s)", post_id, bool(scheduled_unix))
    return post_id


def fb_create_photo_post(image_url: str, caption: Optional[str] = None,
                         scheduled_unix: Optional[int] = None) -> str:
    """
    Post a photo by URL to the Page. Returns post_id.
    If scheduled_unix provided: schedules publication.
    """
    _check_env()
    if not image_url:
        raise MetaError("image_url is required for FB photo post")

    url = f"{GRAPH}/{FB_PAGE_ID}/photos"
    data = {
        "url": image_url,
        "access_token": FB_PAGE_TOKEN,
    }
    if caption:
        data["caption"] = caption
    _apply_fb_schedule(data, scheduled_unix)

    j = _request("POST", url, data=data)
    post_id = j.get("post_id") or j.get("id")
    if not post_id:
        raise MetaError(f"Missing post_id in response: {j}")
    LOG.info("FB photo post id=%s (scheduled=%s)", post_id, bool(scheduled_unix))
    return post_id


def fb_create_video_post(file_url: str, description: Optional[str] = None,
                         scheduled_unix: Optional[int] = None) -> str:
    """
    Post a video by URL to the Page. Returns video_id.
    For file uploads from disk, you would use 'files' with 'source'.
    """
    _check_env()
    if not file_url:
        raise MetaError("file_url is required for FB video post")

    url = f"{GRAPH}/{FB_PAGE_ID}/videos"
    data = {
        "file_url": file_url,
        "access_token": FB_PAGE_TOKEN,
    }
    if description:
        data["description"] = description
    _apply_fb_schedule(data, scheduled_unix)

    j = _request("POST", url, data=data)
    video_id = j.get("id")
    if not video_id:
        raise MetaError(f"Missing video id in response: {j}")
    LOG.info("FB video post id=%s (scheduled=%s)", video_id, bool(scheduled_unix))
    return video_id

