"""Site-specific allauth socialaccount adapter.

Two jobs, both about making settings-configured providers behave like database-configured ones.

**A settings-derived Google app when the database has none.** The web Google login is configured
through a ``SocialApp`` row, but the mobile flow has only ever needed ``GOOGLE_OAUTH_CLIENT_ID`` --
it verifies an ID token rather than exchanging a code. Routing mobile sign-in through allauth's
pipeline means it needs a provider *app* too, so the fallback below is used only when nothing else
defines a Google app.

**Letting a settings-configured provider store its tokens.** ``SOCIALACCOUNT_STORE_TOKENS`` is on,
because Apple's refresh token makes deletion-time revocation possible. Apple and Facebook are
configured in settings rather than as rows, so their ``SocialApp`` has no primary key and Django
refuses to save a foreign key to it. See :meth:`pre_social_login`.
"""

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings


class FishAuctionsSocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        """Drop an unsaved provider app off the token before anything tries to save it.

        ``SocialLogin.save()`` writes ``SocialLogin.token`` whenever ``STORE_TOKENS`` is on, and a
        settings-configured provider's ``SocialApp`` is unsaved -- so Django raises *"save() prohibited ...
        unsaved related object 'app'"* and every Apple and Facebook signup 500s, on the web as much as in
        the app.

        allauth does exactly this in ``SocialLogin._store_token`` for the returning-user path, just not for
        first-time signup or connection. ``SocialToken.app`` is nullable and nothing reads it back -- tokens
        are looked up by account -- so dropping it loses nothing.
        """
        token = getattr(sociallogin, "token", None)
        app = getattr(token, "app", None)
        if app is not None and not app.pk:
            token.app = None
        return super().pre_social_login(request, sociallogin)

    def list_apps(self, request, provider=None, client_id=None):
        """allauth's app list, plus a Google app built from ``GOOGLE_OAUTH_CLIENT_ID``.

        Appended only when the merged database and settings list has no Google app, so it can never produce
        the ambiguity ``get_app`` rejects. It carries no secret: the flow needing one already requires a
        configured app, and verifying a Google ID token is the only flow this serves.
        """
        apps = super().list_apps(request, provider=provider, client_id=client_id)
        google_client_id = getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
        if not google_client_id or provider not in (None, "google"):
            return apps
        if client_id is not None and client_id != google_client_id:
            return apps
        if any(app.provider == "google" or app.provider_id == "google" for app in apps):
            return apps
        from allauth.socialaccount.models import SocialApp

        return [*apps, SocialApp(provider="google", name="Google", client_id=google_client_id, secret="")]
