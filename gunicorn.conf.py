"""Gunicorn configuration for the ASGI (uvicorn) worker.

The primary reason this file exists is to run the uvicorn workers on the stdlib
``asyncio`` event loop instead of ``uvloop``.

Background: production gunicorn workers were dying with SIGABRT (heap
corruption / internal abort) inside uvloop 0.22.1's callback-scheduling path
(``new_Handle`` / ``cb_idle_callback``), reached from the running event loop
under the Django Channels websocket stack. Each crash also dropped a ~200MB
core file into the bind-mounted repo root. Switching the workers to the
stdlib asyncio loop removes uvloop from the hot path entirely; it is the
battle-tested default and the throughput difference is negligible for this
I/O-bound (Redis/DB) app. uvloop stays installed but unused, so this is fully
reversible by deleting this override.
"""

# Bind/worker settings mirror the previous inline `gunicorn ... -w 8` invocation
# so behavior is unchanged apart from the event loop.
bind = "0.0.0.0:8000"
workers = 8
worker_class = "fishauctions.uvicorn_worker.AsyncioUvicornWorker"
