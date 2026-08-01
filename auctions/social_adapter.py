"""Site-specific allauth socialaccount adapter.

Two jobs, both about making settings-configured providers behave like database-configured ones.

**A settings-derived Google app when the database doesn't have one.** The web Google login is
configured through a ``SocialApp`` row in the admin, but the mobile Google flow has only ever needed
``GOOGLE_OAUTH_CLIENT_ID`` — it verifies an ID token rather than exchanging a code, so it never had
a use for the client secret. Routing mobile sign-in through allauth's pipeline
(:mod:`auctions.mobile.services.social_auth`) means it now needs a provider *app* as well, and a
deployment that has the env var but no admin row would otherwise stop being able to sign in with
Google. The fallback below is used only when nothing else defines a Google app, so a deployment
with a real ``SocialApp`` behaves exactly as before.

**Letting a settings-configured provider store its tokens.** ``SOCIALACCOUNT_STORE_TOKENS`` is on,
because Apple's refresh token is what makes deletion-time revocation possible. Apple and Facebook
are configured in ``settings.SOCIALACCOUNT_PROVIDERS`` rather than as database rows, so their
``SocialApp`` has no primary key and Django refuses to save a foreign key pointing at it. See
:meth:`pre_social_login`.
"""

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings


class FishAuctionsSocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        """Drop an unsaved provider app off the token before anything tries to save it.

        ``SocialLogin.save()`` writes ``SocialLogin.token`` whenever ``STORE_TOKENS`` is on, and a
        settings-configured provider's ``SocialApp`` is an unsaved instance — so Django raises
        *"save() prohibited to prevent data loss due to unsaved related object 'app'"* and the whole
        sign-in 500s. It would hit every Apple and Facebook signup, on the web and in the app alike.

        allauth already does exactly this in ``SocialLogin._store_token`` for the returning-user
        path; it just doesn't for first-time signup or account connection, which is what this
        covers. ``SocialToken.app`` is nullable and nothing reads it back — tokens are looked up by
        account — so dropping it loses nothing.

        This hook runs on every social login, before signup and before connect, which is why it
        lives here rather than in any one flow.
        """
        token = getattr(sociallogin, "token", None)
        app = getattr(token, "app", None)
        if app is not None and not app.pk:
            token.app = None
        return super().pre_social_login(request, sociallogin)

    def list_apps(self, request, provider=None, client_id=None):
        """allauth's app list, plus a Google app built from ``GOOGLE_OAUTH_CLIENT_ID``.

        The fallback is appended only when the merged database + settings list contains no Google
        app at all, so it can never produce the "two apps for one provider" ambiguity that
        ``get_app`` rejects. It carries no secret: the flow that needs one (the web OAuth code
        exchange) already requires a properly configured app, and the flow that doesn't (verifying a
        Google ID token, which checks the signature and the audience) is the only one this serves.
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
