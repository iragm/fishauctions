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

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator

from auctions.consumers import LotConsumer, UserConsumer

application = ProtocolTypeRouter(
    {
        # Django's ASGI application to handle traditional HTTP requests
        "http": django_asgi_app,
        # WebSocket chat handler
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(
                URLRouter(
                    [
                        re_path(r"ws/lots/(?P<lot_number>\w+)/$", LotConsumer.as_asgi()),
                        re_path(r"ws/users/(?P<user_pk>\w+)/$", UserConsumer.as_asgi()),
                    ]
                )
            )
        ),
    }
)
