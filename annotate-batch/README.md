# annotate-batch (bare bones web UI)

Simple web wrapper around `annotate-border-v2.py` for batch upload + view/download.

## Features

- Drag-and-drop JPEG upload (`.jpg`, `.jpeg`)
- Annotates each upload using your existing profile/template system
- Lists annotated files with view/download links
- Lightweight per-browser-session isolation (files are separated by session)
- UI clear button to wipe all stored uploads/annotated files and session folders

## Layout

- `app.py` - Flask app
- `uploads/` - stored uploaded source files
- `annotated/` - rendered output files
- `templates/index.html` - minimal UI
- `static/` - CSS + JS
- `annotate-border/annotate-border-v2.py` - V2 annotation engine used by web app
- `annotate-border/profiles/annotation-v2-*.annotate` - V2 profile templates used by web app
- `annotate-border/PROFILE-V2.md` - V2 profile format specification

## Local run

From `annotate-batch/`:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Then open: `http://127.0.0.1:5050`

## Session isolation

Each browser session gets its own upload/output subdirectories under:

- `annotate-batch/uploads/<session_id>/`
- `annotate-batch/annotated/<session_id>/`

So users no longer see each other's file lists or downloads by default.

Set a stable secret in production (required for consistent session cookies across restarts/workers):

```bash
ANNOTATE_SESSION_SECRET='replace-with-a-long-random-secret' .venv/bin/gunicorn wsgi:application --bind localhost:5050
```

## Profile selection

Default profile:

- `annotate-border/profiles/annotation-v2-demo.annotate`

Built-in alternatives:

- `annotate-border/profiles/annotation-v2-125.annotate`

V2 profiles must include `@profile-version 2` and follow the box-model spec in `annotate-border/PROFILE-V2.md`.

Override via environment variable:

```bash
ANNOTATE_PROFILE=/absolute/path/to/profile.annotate .venv/bin/python annotate-batch/app.py
```

## Apache/MAMP notes

Recommended production setup: run the app with Gunicorn and place Apache in front as a reverse proxy.

- Use `annotate-batch/apache-vhost-example.conf` (HTTP) or `annotate-batch/apache-vhost-https-example.conf` (HTTPS) as starting points.
- These examples proxy dynamic requests to `http://127.0.0.1:5050` and serve `/static` directly from Apache.

## Debian/Linode quick checklist (Apache reverse proxy + Gunicorn + HTTPS)

Assume project path:

- `/srv/photography-support`

1) Install packages:

```bash
sudo apt update
sudo apt install -y apache2 python3-venv certbot python3-certbot-apache
```

2) Create app venv + install deps:

```bash
cd /srv/photography-support/annotate-batch
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

3) Enable required Apache modules:

```bash
sudo a2enmod proxy proxy_http headers ssl rewrite
```

4) Start Gunicorn (example):

```bash
cd /srv/photography-support/annotate-batch
ANNOTATE_PROFILE=/srv/photography-support/annotate-border/profiles/annotation-v2-125.annotate \
ANNOTATE_SESSION_SECRET='replace-with-a-long-random-secret' \
.venv/bin/gunicorn wsgi:application --bind 127.0.0.1:5050 --workers 2 --threads 4 --timeout 600
```

5) Install site config:

- Start from `annotate-batch/apache-vhost-https-example.conf`
- Replace placeholders:
  - `blindingmoon.net`
  - `/ABS/PATH/TO/photography-support`

Then copy to Apache sites dir (example name):

```bash
sudo cp annotate-batch/apache-vhost-https-example.conf /etc/apache2/sites-available/annotate-batch.conf
```

6) Enable site + disable default (optional):

```bash
sudo a2ensite annotate-batch.conf
sudo a2dissite 000-default.conf
```

7) Create/verify writable runtime dirs for the Gunicorn user:

```bash
sudo mkdir -p /srv/photography-support/annotate-batch/uploads
sudo mkdir -p /srv/photography-support/annotate-batch/annotated
```

8) Check Apache config + reload:

```bash
sudo apache2ctl configtest
sudo systemctl reload apache2
```

9) Issue TLS cert (Let's Encrypt):

```bash
sudo certbot --apache -d blindingmoon.net
```

10) Reload after certbot updates vhost:

```bash
sudo apache2ctl configtest
sudo systemctl reload apache2
```

11) Verify:

```bash
curl -I http://blindingmoon.net
curl -I https://blindingmoon.net
```

Expected:
- HTTP returns `301` redirect to HTTPS
- HTTPS returns `200` on `/`

### Optional profile override

Set profile at Gunicorn startup (not in Apache):

```bash
ANNOTATE_PROFILE=/srv/photography-support/annotate-border/profiles/annotation-v2-125.annotate
```
