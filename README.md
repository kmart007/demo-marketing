# demo-marketing
top elite demo marketing app


# TRUSECT Social Executor

A tiny Flask + Gunicorn service that works with **Gumloop** to:
- draft social posts,
- collect **owner approval**,
- and publish to **Instagram** (IG Graph API) and **Facebook Pages** on a schedule.

It stores approved content in **S3** (a simple JSON queue), exposes clean endpoints (`/drafts`, `/approve`, `/scheduler/run`), and alternates IG/FB twice daily.

---

## Why this app exists

Marketing teams need a safe, **human-in-the-loop** pipeline:

1. AI drafts a post  
2. Owner clicks “Approve”  
3. Scheduler posts at the right time/channel

This app is the executor behind that flow. Gumloop triggers it with **Call API** nodes.

---

## Endpoints (quick reference)

- `GET /healthz` – liveness probe → `{"ok": true}`
- `POST /drafts` – create a **pending** draft (saved to S3)
- `POST /approve` – mark a draft **approved** (optionally publish now)
- `GET /approve` – email-friendly approval link (`?post_id=...&publish_now=true|false`)
- `POST /scheduler/run?slot=am|pm` – posts the next **approved** item; channel is chosen by `scheduler.py`

---

## Project layout

