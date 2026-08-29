"""WSGI entrypoint for production servers.

Expose Flask app as `application` for Gunicorn/uWSGI/mod_wsgi.
"""

from app import app as application
