"""Custom gunicorn worker that runs uvicorn on the stdlib asyncio loop.

See gunicorn.conf.py for why uvloop is avoided. We subclass uvicorn's
UvicornWorker rather than adding the newer standalone ``uvicorn-worker``
package, keeping this a zero-dependency change.
"""

from uvicorn.workers import UvicornWorker


class AsyncioUvicornWorker(UvicornWorker):
    # UvicornWorker defaults to loop="auto", which selects uvloop when it is
    # installed. Pin the stdlib asyncio loop instead. httptools is kept for the
    # HTTP parser -- the crash was in uvloop, not httptools.
    CONFIG_KWARGS = {"loop": "asyncio", "http": "httptools"}
