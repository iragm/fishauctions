"""What a plugin directory asks of this server, beyond what an MCP client already asks.

Three things, and each fails silently rather than loudly if it drifts: the domain-verification token
(``auctions/mcp/verification.py``), the ChatGPT spelling of the widget pointer
(``auctions/mcp/widgets.py``), and the OpenID layer that carries a verified email address
(``auctions/mcp/oidc.py``). The last one is tested from both sides -- with a key and without -- since
the failure that matters is advertising a scope with nothing behind it.
"""

from __future__ import annotations

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import timedelta
from functools import lru_cache
from hashlib import sha256
from urllib.parse import parse_qs, urlparse

from allauth.account.models import EmailAddress
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_application_model

from auctions.mcp import widgets

Application = get_application_model()
AccessToken = get_access_token_model()

CHALLENGE_URL = "/.well-known/openai-apps-challenge"


@lru_cache(maxsize=1)
def _private_key() -> str:
    """One RSA key for the whole module; generating one per test costs more than the tests do."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def with_oidc():
    """The live OAUTH2_PROVIDER settings as they look on a deployment that has a key."""
    provider = dict(settings.OAUTH2_PROVIDER)
    provider["SCOPES"] = {
        **provider["SCOPES"],
        "openid": "Know which account you signed in with",
        "email": "See the email address on your account",
    }
    provider["OIDC_ENABLED"] = True
    provider["OIDC_RSA_PRIVATE_KEY"] = _private_key()
    return override_settings(OAUTH2_PROVIDER=provider)


def without_oidc():
    """The same for a deployment that has no key -- CI, and every checkout that isn't the one
    being submitted. Pinned rather than inherited from the live settings, which differ per box."""
    provider = dict(settings.OAUTH2_PROVIDER)
    provider["SCOPES"] = {name: text for name, text in provider["SCOPES"].items() if name not in ("openid", "email")}
    provider["OIDC_ENABLED"] = False
    provider["OIDC_RSA_PRIVATE_KEY"] = ""
    return override_settings(OAUTH2_PROVIDER=provider)


class DomainVerificationTests(TestCase):
    """The portal fetches one URL and compares the whole body against the token it issued."""

    @override_settings(OPENAI_APPS_CHALLENGE_TOKEN="")
    def test_an_unconfigured_deployment_has_no_token_rather_than_an_empty_one(self):
        self.assertEqual(self.client.get(CHALLENGE_URL).status_code, 404)

    @override_settings(OPENAI_APPS_CHALLENGE_TOKEN="a-token-from-the-portal")
    def test_the_body_is_the_token_and_nothing_else(self):
        response = self.client.get(CHALLENGE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "a-token-from-the-portal")
        self.assertTrue(response["Content-Type"].startswith("text/plain"))

    @override_settings(OPENAI_APPS_CHALLENGE_TOKEN="a-token-from-the-portal")
    def test_it_answers_without_a_redirect_and_without_signing_in(self):
        # APPEND_SLASH is on site-wide and the portal does not follow redirects.
        response = self.client.get(CHALLENGE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response)


class WidgetPointerTests(TestCase):
    """A widget is named twice, because two hosts read two different keys for the same thing."""

    def test_every_widget_tool_names_its_resource_in_both_dialects(self):
        for name, uri in widgets.TOOL_WIDGETS.items():
            meta = widgets.tool_meta(name)
            self.assertEqual(meta[widgets.RESOURCE_URI_META_KEY], uri, name)
            self.assertEqual(meta[widgets.OPENAI_OUTPUT_TEMPLATE_META_KEY], uri, name)
            self.assertEqual(meta["ui"]["resourceUri"], uri, name)

    def test_a_tool_without_a_widget_carries_no_pointer(self):
        self.assertIsNone(widgets.tool_meta("list_lots"))


class OpenIDTests(TestCase):
    """The two claims, and the two ways the server refuses to half-promise them."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user("oidc-tester", "oidc@example.com", "x")
        EmailAddress.objects.create(user=cls.user, email=cls.user.email, verified=True, primary=True)

    def _token(self, scope="openid email read"):
        application = Application.objects.create(
            name="a directory",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://example.com/callback",
        )
        return AccessToken.objects.create(
            user=self.user,
            application=application,
            token="an-access-token",
            scope=scope,
            expires=timezone.now() + timedelta(hours=1),
        )

    def test_without_a_key_nothing_is_advertised(self):
        with without_oidc():
            document = self.client.get("/.well-known/oauth-authorization-server").json()
            self.assertNotIn("openid", document["scopes_supported"])
            self.assertEqual(self.client.get("/o/.well-known/openid-configuration").status_code, 404)

    def test_the_discovery_document_names_the_userinfo_endpoint_and_both_scopes(self):
        with with_oidc():
            document = self.client.get("/o/.well-known/openid-configuration").json()
        self.assertTrue(document["userinfo_endpoint"].endswith("/o/userinfo/"))
        self.assertIn("openid", document["scopes_supported"])
        self.assertIn("email", document["scopes_supported"])
        self.assertIn("email", document["claims_supported"])
        self.assertIn("email_verified", document["claims_supported"])
        # A public client has no secret to authenticate with, so this list has to say so here too.
        self.assertIn("none", document["token_endpoint_auth_methods_supported"])

    def test_userinfo_returns_the_verified_address(self):
        with with_oidc():
            token = self._token()
            response = self.client.get("/o/userinfo/", headers={"authorization": f"Bearer {token.token}"})
        self.assertEqual(response.status_code, 200)
        claims = response.json()
        self.assertEqual(claims["email"], "oidc@example.com")
        self.assertIs(claims["email_verified"], True)
        self.assertEqual(claims["sub"], str(self.user.pk))

    def test_an_unconfirmed_address_says_so(self):
        EmailAddress.objects.filter(user=self.user).update(verified=False)
        with with_oidc():
            token = self._token()
            response = self.client.get("/o/userinfo/", headers={"authorization": f"Bearer {token.token}"})
        self.assertIs(response.json()["email_verified"], False)

    def test_a_token_without_the_email_scope_gets_no_email(self):
        with with_oidc():
            token = self._token(scope="openid read")
            response = self.client.get("/o/userinfo/", headers={"authorization": f"Bearer {token.token}"})
        self.assertNotIn("email", response.json())

    def test_a_registered_client_can_sign_an_id_token(self):
        """Registration never sets an algorithm, and an unsigned header fails at the token endpoint."""
        with with_oidc():
            token = self._token()
        self.assertEqual(token.application.algorithm, Application.RS256_ALGORITHM)

    def test_the_whole_flow_mints_an_id_token(self):
        """The end a directory actually walks: consent, code, token, and something signed in it.

        The algorithm is only ever read here, at the far end of a flow a person is standing in the
        middle of, which is why it can't be left to be noticed in production.
        """
        verifier = "a" * 64
        challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        with with_oidc():
            application = Application.objects.create(
                name="a directory",
                client_type=Application.CLIENT_PUBLIC,
                authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
                redirect_uris="https://example.com/callback",
            )
            self.client.force_login(self.user)
            granted = self.client.post(
                "/o/authorize/",
                {
                    "client_id": application.client_id,
                    "response_type": "code",
                    "redirect_uri": "https://example.com/callback",
                    "scope": "openid email read",
                    "state": "s",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "allow": "Authorize",
                },
            )
            self.assertEqual(granted.status_code, 302, granted.content[:400])
            code = parse_qs(urlparse(granted["Location"]).query)["code"][0]
            exchanged = self.client.post(
                "/o/token/",
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "https://example.com/callback",
                    "client_id": application.client_id,
                    "code_verifier": verifier,
                },
            )
        self.assertEqual(exchanged.status_code, 200, exchanged.content[:400])
        payload = exchanged.json()
        self.assertIn("id_token", payload)
        header = json.loads(urlsafe_b64decode(payload["id_token"].split(".")[0] + "=="))
        self.assertEqual(header["alg"], "RS256")

    def test_a_client_registered_with_oidc_off_is_left_alone(self):
        with without_oidc():
            token = self._token()
        self.assertEqual(token.application.algorithm, Application.NO_ALGORITHM)
