# chat/routing.py
from django.urls import re_path

from . import consumers

print('routing')

websocket_urlpatterns = [
    re_path(r'ws/lots/(?P<lot_number>\w+)/$', consumers.LotConsumer.as_asgi()),
]