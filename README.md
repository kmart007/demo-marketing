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
```
/opt/social-executor/
├─ app.py                 # Flask app / routes
├─ meta_client.py         # IG & FB Graph API helpers
├─ queue_store_s3.py      # S3-backed queue store
├─ scheduler.py           # am/pm channel alternation (TZ-aware)
├─ requirements.txt
├─ .env                   # local environment (dev convenience)
└─ .venv/                 # Python virtual environment (created at setup)
```
---

## Requirements

- Python 3.10+ (tested on 3.12)
- AWS IAM role on the instance with RW to your S3 object (the queue JSON)
- EC2 Security Group allowing inbound 443 to Nginx/ALB (prod)
(Dev-only shortcut: temporarily expose 8000 to your IP.)

---

##Environment variables (.env)

Create .env in the project root with at least:
```
# Meta / Graph API
META_API_VERSION=v23.0
IG_USER_ID=1789xxxxxxxxxxxx           # Instagram Business/Creator Account ID
FB_PAGE_ID=1xxxxxxxxxxxxxxx           # Facebook Page ID
IG_ACCESS_TOKEN=EAAG...               # Long-lived user token
FB_PAGE_ACCESS_TOKEN=EAAG...          # Page access token

# Scheduler / Timezone
TZ=America/New_York
# Optional: flip odd-day morning channel (instagram|facebook)
# SCHEDULER_ODD_AM=instagram

# S3 queue location
QUEUE_S3_BUCKET=trusect-social-ops
QUEUE_S3_KEY=social/approved_posts.json
RECENT_COOLDOWN_DAYS=3
```

In production, prefer AWS Systems Manager Parameter Store or Secrets Manager instead of .env. The code uses python-dotenv for dev convenience.

---

## How to run (development)
# 1) Get code & create venv
```
cd /opt/social-executor
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt gunicorn

# 2) Add your .env (see above)

# 3) Start the API (foreground)
python -m gunicorn --chdir /opt/social-executor -w 3 -b 127.0.0.1:8000 app:app

# 4) In a second shell, test:
curl -sS http://127.0.0.1:8000/healthz
```

Smoke test the queue (optional):
```
curl -sS -X POST http://127.0.0.1:8000/drafts \
  -H 'Content-Type: application/json' \
  -d '{"caption":"hello from dev","media_type":"image","platforms":["instagram","facebook"]}'
```

---


## How to run (production via systemd)

Create the unit file:
```
# /etc/systemd/system/social-executor.service
[Unit]
Description=TRUSECT Social Executor (Flask via Gunicorn)
After=network.target

[Service]
User=socialapp
WorkingDirectory=/opt/social-executor
Environment="PATH=/opt/social-executor/.venv/bin:/usr/local/bin:/usr/bin"
ExecStart=/opt/social-executor/.venv/bin/python -m gunicorn \
  --chdir /opt/social-executor \
  -w 3 -b 127.0.0.1:8000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable & start:
```
sudo systemctl daemon-reload
sudo systemctl enable --now social-executor
sudo systemctl status social-executor -n 50
```
HTTPS (recommended)

Terminate TLS at Nginx (or an Application Load Balancer) and proxy to 127.0.0.1:8000.
Minimal Nginx server (after you obtain a cert via certbot or ACM on an ALB):
```
server {
  listen 80;
  server_name social.example.com;
  return 301 https://$host$request_uri;
}
server {
  listen 443 ssl;
  server_name social.example.com;

  ssl_certificate     /etc/letsencrypt/live/social.example.com/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/social.example.com/privkey.pem;

  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
  }
}
```

---

## Why we moved the service to /opt
```
We originally ran from /home/socialapp/..., but on many RHEL/enterprise setups:
- /home can be mounted with noexec → executables in /home won’t run (systemd error: “Failed at step EXEC … Permission denied”).
- SELinux contexts in /home can block service execution.

/opt is the standard location for custom apps and allows execution by default. Moving the project there (and recreating the venv under /opt) ensures:
- systemd can execute /opt/social-executor/.venv/bin/python
- fewer SELinux policy surprises
```
a cleaner separation from user home directories

If you must stay in /home, you’d need to remount without noexec and ensure SELinux contexts are correct (restorecon -Rv …). Using /opt is simpler and safer.

---

##Basic troubleshooting

- Service won’t start; Permission denied at EXEC: likely /home noexec. Move to /opt (see above).
- ModuleNotFoundError: app: set WorkingDirectory correctly or use --chdir /opt/social-executor.
- S3 JSON decode error: initialize the queue object to {"posts":[]} or update queue_store_s3.py (auto-heals empty files).

- Publish fails: verify IG_USER_ID, FB_PAGE_ID, and tokens; Facebook Page token must match the Page you’re posting to; IG uses long-lived user token (not Basic Display).

- Health check: curl https://your-domain/healthz should return {"ok": true}.

---

## Test matrix (curl)
```
# Health
curl -sS http://127.0.0.1:8000/healthz

# Create a draft
curl -sS -X POST http://127.0.0.1:8000/drafts \
  -H 'Content-Type: application/json' \
  -d '{"caption":"first post","media_url":"https://picsum.photos/1200","media_type":"image","platforms":["instagram","facebook"],"source":"manual"}'

# Approve (no auto-publish)
curl -sS "http://127.0.0.1:8000/approve?post_id=DRAFT_ID&publish_now=false"

# Run scheduler (AM or PM)
curl -sS -X POST "http://127.0.0.1:8000/scheduler/run?slot=am"
```

---


## Connecting Gumloop (high level)

- Owner Input flow: Interface → Ask AI → Call API POST /drafts → Email with GET /approve?....

- Auto-Poster flow: Time triggers (09:30 & 17:30 ET) → Call API POST /scheduler/run?slot=am|pm.

- Weekly Generator: Time trigger (Mon 09:00 ET) → 2× Ask AI → 2× Call API /drafts → Email with two approve links.

Once your domain is live over HTTPS (Nginx/ALB), point Gumloop nodes to:
```
https://social.your-domain.com/drafts
https://social.your-domain.com/approve?post_id=...
https://social.your-domain.com/scheduler/run?slot=am
```

---

## License / ownership

This template is yours to modify and deploy for your team. Add a LICENSE file if you plan to redistribute.

---

## Support

Open your service logs and share any stack traces if you need help:
```
sudo journalctl -u social-executor -n 200 -f
```
