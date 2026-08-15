"""WSGI entrypoint for Apache mod_wsgi.

Expose Flask app as `application` for mod_wsgi.
"""

from app import app as application
