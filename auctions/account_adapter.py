"""Site-specific allauth account adapter: which address its rate limits count against, and who its
emails greet.

allauth counts ``login``, ``login_failed``, ``signup`` and ``reset_password`` per IP, and works that
address out with its own helper rather than :mod:`auctions.client_ip`. Left alone that helper
disregards ``X-Forwarded-For`` and falls back to ``REMOTE_ADDR`` -- which behind nginx is the nginx
container, the same value for every visitor. Every one of those limits was one bucket shared by the
whole site: a room checking in together could lock each other out, and no single caller was ever
limited on their own.

``ALLAUTH_TRUSTED_CLIENT_IP_HEADER`` would point it at the right header, but it replaces the fallback
instead of preceding it, and the adapter raises ``PermissionDenied`` when it comes back with nothing
-- so a request that never went through nginx (the Selenium tests, a health check, anything hitting
the origin directly) would 403 on every account page. Here the two are in order: our header first,
allauth's own answer if there isn't one.

allauth renders its email with the request, so the ``auth`` context processor supplies ``user``: the
signed-in visitor, not the recipient. A mail about an address with no account (``unknown_account``,
``account_already_exists``) passes no ``user`` of its own, and would greet whoever asked -- a
stranger's name, sent to someone else. :meth:`render_mail` pins it to nobody unless allauth said who.
"""

from allauth.account.adapter import DefaultAccountAdapter

from auctions.client_ip import client_ip


class FishAuctionsAccountAdapter(DefaultAccountAdapter):
    def get_client_ip(self, request):
        return client_ip(request) or super().get_client_ip(request)

    def render_mail(self, template_prefix, email, context, headers=None):
        return super().render_mail(template_prefix, email, {"user": None, **context}, headers)
