"""Gunicorn configuration for the ASGI (uvicorn) worker.

It exists to run the workers on the stdlib ``asyncio`` event loop instead of ``uvloop``. Production
workers were dying with SIGABRT inside uvloop 0.22.1's callback-scheduling path (``new_Handle`` /
``cb_idle_callback``), reached from the running event loop under Django Channels, each crash also
dropping a ~200MB core file into the bind-mounted repo root. The stdlib loop takes uvloop out of the
hot path entirely and the throughput difference is negligible for an I/O-bound app. uvloop stays
installed but unused, so deleting this file reverses it.
"""

# Bind/worker settings mirror the previous inline `gunicorn ... -w 8` invocation
# so behavior is unchanged apart from the event loop.
bind = "0.0.0.0:8000"
workers = 8
worker_class = "fishauctions.uvicorn_worker.AsyncioUvicornWorker"
