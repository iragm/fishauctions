# isort: skip_file
# ruff: noqa: E402
import os

from django.core.asgi import get_asgi_application
from django.urls import re_path

# Fetch Django ASGI application early to ensure AppRegistry is populated
# before importing consumers and AuthMiddlewareStack that may import ORM
# models.
# ruff: noqa: E402
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fishauctions.settings")
django_asgi_app = get_asgi_application()

import logging  # noqa: E402

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator

from auctions.consumers import LotConsumer, UserConsumer, AuctionConsumer  # noqa: E402

websocket_error_logger = logging.getLogger("auctions.websocket")


class LogWebsocketExceptions:
    """Email admins on unhandled exceptions in the websocket app.

    ASGI exceptions otherwise only reach uvicorn's own logger and never hit
    Django's AdminEmailHandler, so a crashing consumer (e.g. the channel-layer
    read timeout that silently broke in-person bidding) failed invisibly. This
    logs with a traceback to the ``auctions.websocket`` logger -- which is wired
    to mail_admins -- then re-raises so connection behavior is unchanged. Only
    ``Exception`` is caught, so normal task cancellation (CancelledError, a
    BaseException) passes through untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        try:
            return await self.app(scope, receive, send)
        except Exception:
            websocket_error_logger.exception("Unhandled exception in websocket ASGI app (path=%s)", scope.get("path"))
            raise


application = ProtocolTypeRouter(
    {
        # Django's ASGI application to handle traditional HTTP requests
        "http": django_asgi_app,
        # WebSocket chat handler
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(
                LogWebsocketExceptions(
                    URLRouter(
                        [
                            re_path(r"ws/lots/(?P<lot_number>\w+)/$", LotConsumer.as_asgi()),
                            re_path(r"ws/users/(?P<user_pk>\w+)/$", UserConsumer.as_asgi()),
                            re_path(r"ws/auctions/(?P<auction_pk>\w+)/$", AuctionConsumer.as_asgi()),
                        ]
                    )
                )
            )
        ),
    }
)
