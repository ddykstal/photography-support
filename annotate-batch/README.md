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
- `bin/annotate-border-v2.py` - V2 annotation engine used by web app
- `bin/profiles/annotation-v2-*.annotate` - V2 profile templates used by web app
- `PROFILE-V2.md` - V2 profile format specification

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

- `annotate-batch/bin/profiles/annotation-v2-demo.annotate`

Built-in alternatives:

- `annotate-batch/bin/profiles/annotation-v2-125.annotate`

V2 profiles must include `@profile-version 2` and follow the box-model spec in `PROFILE-V2.md`.

Override via environment variable:

```bash
ANNOTATE_PROFILE=/absolute/path/to/profile.annotate .venv/bin/python annotate-batch/app.py
```

## Apache/MAMP notes

This app is WSGI-compatible as `application` in `annotate-batch/wsgi.py`.

- For Apache on Debian, use `mod_wsgi` and point to `annotate-batch/wsgi.py`.
- In MAMP Pro, either proxy to a Python process or use a WSGI setup depending on your stack.

You said you'll provide server definitions, so this repo keeps only app code and runtime folders.

## Debian/Linode quick checklist (Apache + mod_wsgi + HTTPS)

Assume project path:

- `/srv/photography-support`

1) Install packages:

```bash
sudo apt update
sudo apt install -y apache2 libapache2-mod-wsgi-py3 python3-venv certbot python3-certbot-apache
```

2) Create app venv + install deps:

```bash
cd /srv/photography-support/annotate-batch
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

3) Enable required Apache modules:

```bash
sudo a2enmod wsgi ssl headers rewrite
```

4) Install site config:

- Start from `annotate-batch/apache-vhost-https-example.conf`
- Replace placeholders:
  - `blindingmoon.net`
  - `/ABS/PATH/TO/photography-support`

Then copy to Apache sites dir (example name):

```bash
sudo cp annotate-batch/apache-vhost-https-example.conf /etc/apache2/sites-available/annotate-batch.conf
```

5) Enable site + disable default (optional):

```bash
sudo a2ensite annotate-batch.conf
sudo a2dissite 000-default.conf
```

6) Create/verify writable runtime dirs for Apache user (`www-data`):

```bash
sudo mkdir -p /srv/photography-support/annotate-batch/uploads
sudo mkdir -p /srv/photography-support/annotate-batch/annotated
sudo chown -R www-data:www-data /srv/photography-support/annotate-batch/uploads /srv/photography-support/annotate-batch/annotated
```

7) Check Apache config + reload:

```bash
sudo apache2ctl configtest
sudo systemctl reload apache2
```

8) Issue TLS cert (Let's Encrypt):

```bash
sudo certbot --apache -d blindingmoon.net
```

9) Reload after certbot updates vhost:

```bash
sudo apache2ctl configtest
sudo systemctl reload apache2
```

10) Verify:

```bash
curl -I http://blindingmoon.net
curl -I https://blindingmoon.net
```

Expected:
- HTTP returns `301` redirect to HTTPS
- HTTPS returns `200` on `/`

### Optional profile override in Apache

Inside your vhost, set a specific annotation profile:

```apache
WSGISetEnv ANNOTATE_PROFILE /srv/photography-support/annotate-batch/bin/profiles/annotation-v2-125.annotate
```
