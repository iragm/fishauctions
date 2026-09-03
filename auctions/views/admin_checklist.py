"""The admin setup checklist: the one page that says what a new site still needs.

``AdminSetupChecklistView`` is the largest single view in the codebase, which is why it has a module
to itself -- it walks every piece of site configuration and reports what is missing.
"""

import logging
from pathlib import Path

from django.conf import settings
from django.db.models.base import Model as Model
from django.urls import reverse
from django.views.generic import TemplateView

from auctions.site_setup import get_server_public_ip

from .base import AdminOnlyViewMixin

logger = logging.getLogger(__name__)


class AdminSetupChecklistView(AdminOnlyViewMixin, TemplateView):
    template_name = "auctions/admin_setup_checklist.html"

    @staticmethod
    def _yes_no(value):
        return "True" if value else "False"

    @staticmethod
    def _apple_sign_in_items(base_url, site_host):
        """Sign in with Apple: the app half, the web half, and the things Apple requires of both.

        Split up because these fail independently and a deployment can legitimately stop after the
        first. The bundle id alone is a complete, working native sign-in — verifying an Apple
        identity token only needs Apple's public keys.
        """
        from auctions.apple_notifications import notifications_configured
        from auctions.apple_signin import revocation_configured

        section = "Sign in with Apple"
        callback = reverse("apple_callback")
        notification_url = f"{base_url}{reverse('apple_server_notifications')}"
        return [
            {
                "section": section,
                "name": "Sign in with Apple in the mobile app",
                "configured": bool(settings.APPLE_SIGN_IN_BUNDLE_ID),
                "what_it_does": (
                    "Adds &ldquo;Sign in with Apple&rdquo; to the app's login screen. Apple requires this on iOS for any "
                    "app that offers another social login. This one value is enough on its own &mdash; verifying an "
                    "Apple token only needs Apple's public keys, which the server fetches automatically."
                ),
                "where_to_get_it": (
                    "Your app's <strong>Bundle ID</strong>. Not the Services ID below &mdash; they are different "
                    "strings, and confusing the two is the most common way an Apple integration fails."
                ),
                "setup_steps": [
                    "Open <strong>Certificates, Identifiers &amp; Profiles &rarr; Identifiers</strong> and click your app's App ID.",
                    "Tick <strong>Sign in with Apple</strong>, then <strong>Save</strong>.",
                    "Copy the Bundle ID exactly as shown.",
                ],
                "snippets": [{"code": 'APPLE_SIGN_IN_BUNDLE_ID="com.fishauctions.app"'}],
                "links": [{"label": "Apple Developer — Identifiers", "url": "https://developer.apple.com/account"}],
            },
            {
                "section": section,
                "name": "Sign in with Apple on the website",
                "configured": bool(
                    settings.APPLE_SIGN_IN_SERVICES_ID
                    and settings.APPLE_SIGN_IN_KEY_ID
                    and settings.APPLE_SIGN_IN_PRIVATE_KEY
                ),
                "what_it_does": (
                    "Adds the same button to the website, so someone who created their account in the app can sign in "
                    "on a computer and land on the same account. Optional: skip it and the app button still works."
                ),
                "where_to_get_it": (
                    "A <strong>Services ID</strong> (a second identifier, separate from the bundle id above) plus your "
                    "Team ID, a Key ID, and the <code>.p8</code> private key. Put the <code>.p8</code> file next to "
                    "<code>.env</code> and give just its filename."
                ),
                "setup_steps": [
                    "<strong>Identifiers &rarr; +</strong> &rarr; <strong>Services IDs</strong>. Pick a reverse-DNS "
                    "identifier that is <em>not</em> your bundle id, e.g. <code>fish.auction.signin</code>.",
                    "Open it, tick <strong>Sign in with Apple</strong> &rarr; <strong>Configure</strong>. Set the "
                    "Primary App ID, add your domain, and set the return URL to "
                    f"<code>{base_url}{callback}</code>. "
                    "<strong>Note the path</strong> &mdash; there is no <code>/accounts/</code> prefix on this site, "
                    "unlike most allauth documentation. Getting it wrong gives an <code>invalid_client</code> error "
                    "from Apple with no explanation. Apple also rejects <code>http://</code>, so this can't be tested "
                    "on localhost.",
                    "<strong>Keys &rarr; +</strong>, tick <strong>Sign in with Apple</strong>, configure it against "
                    "your App ID, and register. <strong>Download the <code>.p8</code> &mdash; Apple lets you do that "
                    "exactly once.</strong> Note the Key ID shown on that page; your Team ID is top-right of the "
                    "developer site.",
                ],
                "snippets": [
                    {
                        "code": (
                            'APPLE_SIGN_IN_SERVICES_ID="fish.auction.signin"\n'
                            'APPLE_SIGN_IN_TEAM_ID="ABCDE12345"\n'
                            'APPLE_SIGN_IN_KEY_ID="FGHIJ67890"\n'
                            'APPLE_SIGN_IN_KEY_FILE="AuthKey_FGHIJ67890.p8"'
                        )
                    }
                ],
                "links": [{"label": "Apple Developer — Keys", "url": "https://developer.apple.com/account"}],
            },
            {
                "section": section,
                "name": "Account deletion & Hide My Email",
                "configured": revocation_configured(),
                "what_it_does": (
                    "Two things Apple requires of any site offering Sign in with Apple. Both fail <em>silently</em>, "
                    "which is why they get their own entry:"
                    "<ul class='mb-0'>"
                    "<li><strong>Deleting an account must revoke the Apple grant.</strong> That call needs the team "
                    "key above, so set those values even if you skip the website button. Without it, deletion is "
                    "incomplete by Apple's rules and is an App Review item.</li>"
                    "<li><strong>Hide My Email needs your sending domain registered with Apple.</strong> Until it is, "
                    "every message to a <code>@privaterelay.appleid.com</code> address is <strong>discarded without a "
                    "bounce or an error</strong> &mdash; no confirmation email, no invoice, no outbid notice. The user "
                    "simply never hears from the site again.</li>"
                    "</ul>"
                ),
                "where_to_get_it": (
                    "The revocation half is checked automatically above. The email domain can't be checked from here "
                    "&mdash; register it and send yourself a test."
                ),
                "setup_steps": [
                    "Open <strong>Certificates, Identifiers &amp; Profiles &rarr; More &rarr; Configure Sign in with "
                    "Apple for Email Communication</strong>.",
                    f"Under <strong>Email Sources</strong> add your domain (<code>{site_host}</code>) and the exact "
                    f"<code>DEFAULT_FROM_EMAIL</code> address (<code>{settings.DEFAULT_FROM_EMAIL}</code>).",
                    "Click <strong>Verify</strong> &mdash; Apple checks SPF. Fix SPF first if it fails.",
                    "Test it: sign in with Apple choosing <em>Hide My Email</em>, and confirm the email arrives.",
                ],
                "links": [
                    {
                        "label": "Apple — Configure email communication",
                        "url": "https://developer.apple.com/account/resources/services/configure",
                    }
                ],
            },
            {
                "section": section,
                "name": "Server-to-server notifications",
                # Green once the endpoint can actually verify a notification, which needs an
                # identifier to check the audience against. Whether Apple has been *told* the URL
                # can't be seen from here — that half is the setup steps below.
                "configured": notifications_configured(),
                "what_it_does": (
                    "Apple tells this site when someone disconnects the app, deletes their Apple ID, or turns "
                    "<em>Hide My Email</em> forwarding off. <strong>Apple sends each of these exactly once and keeps no "
                    "history</strong>, so until the URL is registered they are not delayed &mdash; they are lost. "
                    "Without it: people who revoked the app stay signed in, an account whose Apple ID was deleted "
                    "becomes an unreachable ghost nobody can sign into, and mail keeps being sent to a relay address "
                    "that has been throwing it away. It is also how Apple expects a site to keep sessions valid "
                    "without re-checking every account against Apple on a timer."
                ),
                "where_to_get_it": (
                    "Nothing to add to <code>.env</code> &mdash; the endpoint is built in and uses the identifiers "
                    "above. It just has to be registered with Apple, in the same place the web return URL is set."
                ),
                "setup_steps": [
                    "Open <strong>Certificates, Identifiers &amp; Profiles &rarr; Identifiers</strong> and click your "
                    "App ID (or the Services ID, if that is where you configured Sign in with Apple).",
                    "Next to <strong>Sign in with Apple</strong> click <strong>Edit</strong> / "
                    "<strong>Configure</strong>.",
                    f"Put <code>{notification_url}</code> in <strong>Server to Server Notification Endpoint</strong> "
                    "and save. Apple requires https and will not accept a localhost address, so this can only be set "
                    "on a live site.",
                    "Check it: opening that URL in a browser answers <code>ok</code>. Apple itself only ever POSTs to "
                    "it, and a real notification is recorded in the log as <code>apple_notifications</code>.",
                ],
                "links": [
                    {
                        "label": "Apple — Processing changes for Sign in with Apple accounts",
                        "url": (
                            "https://developer.apple.com/documentation/signinwithapple/"
                            "processing-changes-for-sign-in-with-apple-accounts"
                        ),
                    }
                ],
            },
        ]

    @staticmethod
    def _facebook_login_items(base_url):
        section = "Facebook Login"
        callback = reverse("facebook_callback")
        return [
            {
                "section": section,
                "name": "Facebook Login",
                "configured": bool(settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET),
                "what_it_does": (
                    "Adds &ldquo;Continue with Facebook&rdquo; to the app and the website. "
                    "<strong>Expect most Facebook users to arrive without an email address</strong> &mdash; Facebook "
                    "often supplies none, and never confirms the one it does supply. Those users are asked to choose "
                    "an address and confirm it. That is deliberate: trusting an unconfirmed Facebook address would let "
                    "someone claim an existing account by putting that address on a Facebook profile."
                ),
                "where_to_get_it": (
                    "The App ID and App Secret from your Facebook app's <strong>Basic settings</strong>. "
                    "<strong>Facebook is the one provider a fork can't configure from <code>.env</code> alone</strong>: "
                    "the mobile SDKs read the app id from <code>Info.plist</code> / <code>AndroidManifest.xml</code> at "
                    "launch, so it is also compiled into the app build. The value here decides whether the button is "
                    "<em>offered</em> and must match what the app was built with."
                ),
                "setup_steps": [
                    "Create an app, use case <strong>Authenticate and request data from users with Facebook Login</strong>.",
                    "<strong>App settings &rarr; Basic</strong>: copy the App ID and reveal the App Secret. Fill in the "
                    "Privacy Policy URL and User data deletion URL &mdash; Facebook won't let the app go live without them.",
                    "<strong>Use cases &rarr; Authentication</strong>: make sure <code>email</code> and "
                    "<code>public_profile</code> are added. <code>email</code> needs no App Review.",
                    "<strong>Facebook Login &rarr; Settings</strong>: add "
                    f"<code>{base_url}{callback}</code> to Valid OAuth Redirect URIs "
                    "(again, no <code>/accounts/</code> prefix).",
                    "Flip the app <strong>Live</strong>. While it is in Development mode only accounts listed under "
                    "App roles can sign in.",
                ],
                "snippets": [
                    {
                        "code": 'FACEBOOK_APP_ID="1234567890123456"\nFACEBOOK_APP_SECRET="0123456789abcdef0123456789abcdef"'
                    }
                ],
                "links": [{"label": "Facebook — My Apps", "url": "https://developers.facebook.com/apps"}],
            },
        ]

    @staticmethod
    def _tap_to_pay_items():
        """Tap to Pay on iPhone: the Apple-side steps that sit on top of a working Square connection.

        Only shown once Square is set up, because Tap to Pay charges through Square and none of this
        means anything without it.
        """
        from post_office.models import EmailTemplate

        from auctions.management.commands.tap_to_pay_launch_announcement import EMAIL_TEMPLATE_NAME

        if not settings.SQUARE_APPLICATION_ID:
            return []
        section = "Tap to Pay on iPhone"
        return [
            {
                "section": section,
                "name": "Apple's publishing entitlement",
                # Not a .env value — an Apple-side request. "Done" here means "nothing to configure
                # in this file", the same way the branding item does.
                "configured": True,
                "what_it_does": (
                    "<strong>Nothing to configure here, and this page can't tell whether Apple has granted it.</strong> "
                    "Lets merchants take card payments by tapping the card on an iPhone, charging through their "
                    "connected Square account. <strong>This is an App Store step, not a setting on this page.</strong> "
                    "The <em>development</em> entitlement does not cover distribution: TestFlight and the App Store "
                    "need the separate <em>publishing</em> entitlement, which Apple grants only after reviewing the app "
                    "against its Tap to Pay requirements. Apple asks for a screen recording of onboarding and a checkout."
                ),
                "where_to_get_it": "Request it from Apple; the review is on their side and takes time.",
                "links": [
                    {
                        "label": "Request the entitlement",
                        "url": "https://developer.apple.com/contact/request/tap-to-pay-on-iphone/",
                    }
                ],
            },
            {
                "section": section,
                "name": "Launch email & push notification",
                "configured": EmailTemplate.objects.filter(name=EMAIL_TEMPLATE_NAME).exists(),
                "what_it_does": (
                    "Apple's marketing requirements ask for a launch email and an in-app push to every eligible "
                    "merchant, due <strong>once the feature is generally available</strong> &mdash; not at first "
                    "release. The wording and artwork for both <strong>must</strong> come from Apple's Tap to Pay "
                    "Marketing Guide and Toolkit; the guide forbids writing your own. The command below therefore "
                    "refuses to send until that copy is in place, rather than sending something that would fail review."
                ),
                "where_to_get_it": (
                    "The toolkit's access page and password are on <strong>page 23 of the review guide PDF</strong> "
                    "Apple sends with the development entitlement."
                ),
                "setup_steps": [
                    "Download the &ldquo;Launch email&rdquo; template and the &ldquo;Value Proposition&rdquo; "
                    "push-notification copy from Apple's toolkit.",
                    f"Create an email template named <code>{EMAIL_TEMPLATE_NAME}</code> in the admin and paste the "
                    "launch email into it.",
                    "Run the command below with the push copy. Re-running is safe &mdash; nobody is contacted twice.",
                ],
                "snippets": [
                    {
                        "label": "Check who would be contacted, without sending:",
                        "code": "docker exec -it django python3 manage.py tap_to_pay_launch_announcement --dry-run",
                    },
                    {
                        "label": "Send it:",
                        "code": (
                            "docker exec -it django python3 manage.py tap_to_pay_launch_announcement \\\n"
                            '  --push-title "<from the toolkit>" --push-body "<from the toolkit>"'
                        ),
                    },
                ],
                "links": [
                    {
                        "label": "Email templates",
                        "url": "/admin/post_office/emailtemplate/",
                    }
                ],
            },
        ]

    def get_context_data(self, **kwargs):
        from urllib.parse import urlsplit

        from auctions import palette_assist
        from auctions.llm import assist_enabled
        from fishauctions._env import env_has_real_value

        context = super().get_context_data(**kwargs)

        # Asked through the same helper the palette itself uses, so the checklist can never claim
        # the assistant is on while the palette quietly treats it as off.
        llm_configured = assist_enabled()
        llm_window_max = palette_assist.WINDOW_MAX_CALLS
        llm_window_minutes = palette_assist.WINDOW_SECONDS // 60

        server_ip = get_server_public_ip()
        if server_ip:
            domain_help = (
                "Register a domain name with a DNS provider, then create DNS records "
                f"(an A record) that point to this server's IP: <code>{server_ip}</code>"
            )
        else:
            domain_help = (
                "Register a domain name with a DNS provider, then create DNS records "
                "(an A record) that point to this server's public IP address."
            )

        # Build https://your-host from SITE_DOMAIN so the payment redirect/webhook
        # URLs below are copy-paste ready for this install.
        raw_domain = (settings.SITE_DOMAIN or "example.com").strip() or "example.com"
        site_host = urlsplit(raw_domain if "://" in raw_domain else f"//{raw_domain}").hostname or raw_domain
        base_url = f"https://{site_host}"

        email_configured = (
            settings.POST_OFFICE_EMAIL_BACKEND == "django_ses.SESBackend"
            and env_has_real_value(settings.AWS_ACCESS_KEY_ID)
            and env_has_real_value(settings.AWS_SECRET_ACCESS_KEY)
        ) or (env_has_real_value(settings.EMAIL_HOST_USER) and env_has_real_value(settings.EMAIL_HOST_PASSWORD))

        context["setup_items"] = [
            # -- Core setup -------------------------------------------------------
            {
                "section": "Core setup",
                "name": "Site domain",
                "configured": bool((settings.SITE_DOMAIN or "").strip() and settings.SITE_DOMAIN != "example.com"),
                "what_it_does": (
                    "Used in absolute URLs, routed email senders, and the production HTTPS certificate. "
                    "Enter just the hostname &mdash; no <code>https://</code> and no trailing slash."
                ),
                "where_to_get_it": domain_help,
                "snippets": [{"code": 'SITE_DOMAIN="example.com"'}],
            },
            {
                "section": "Core setup",
                "name": "Single club mode",
                # A preference, not a credential: either value is valid, so always "Done".
                "configured": True,
                "what_it_does": (
                    "On by default: the site runs as one club (named after <code>NAVBAR_BRAND</code>) with every user "
                    "auto-added as a member. Set to <code>False</code> only if you host multiple clubs on one install. "
                    f"Currently <strong>{'on' if getattr(settings, 'SINGLE_CLUB_MODE', False) else 'off'}</strong>."
                ),
                "snippets": [
                    {"code": f'SINGLE_CLUB_MODE="{self._yes_no(getattr(settings, "SINGLE_CLUB_MODE", False))}"'}
                ],
            },
            {
                "section": "Core setup",
                "name": "Site identity & branding",
                # A preference, not a credential: always "Done", the help text explains it.
                "configured": True,
                "what_it_does": (
                    "Branding shown across the site and on emails:"
                    "<ul class='mb-0'>"
                    "<li><code>NAVBAR_BRAND</code> &mdash; shown at the top of every page (also the single club's name).</li>"
                    "<li><code>COPYRIGHT_MESSAGE</code> &mdash; shown in the footer. HTML is allowed.</li>"
                    "<li><code>MAILING_ADDRESS</code> &mdash; your physical address, shown next to the unsubscribe link on promo emails (required by anti-spam law).</li>"
                    "<li><code>WEBSITE_FOCUS</code> &mdash; the plural, lowercase noun your site is about, e.g. <code>fish</code>, <code>birds</code>, <code>items</code>.</li>"
                    "<li><code>I_BRED_THIS_FISH_LABEL</code> &mdash; the label shown next to the &ldquo;breeder points&rdquo; checkbox.</li>"
                    "<li><code>WEEKLY_PROMO_MESSAGE</code> &mdash; extra text included in the weekly promotional email (plain text only; usually left blank).</li>"
                    "</ul>"
                ),
                "snippets": [
                    {
                        "code": (
                            f'NAVBAR_BRAND="{settings.NAVBAR_BRAND}"\n'
                            'COPYRIGHT_MESSAGE="Website copyright your club"\n'
                            'MAILING_ADDRESS="123 Your Street, Anytown, USA"\n'
                            f'WEBSITE_FOCUS="{settings.WEBSITE_FOCUS}"\n'
                            f'I_BRED_THIS_FISH_LABEL="{settings.I_BRED_THIS_FISH_LABEL}"\n'
                            'WEEKLY_PROMO_MESSAGE=""'
                        )
                    }
                ],
            },
            {
                "section": "Core setup",
                "name": "Who can create auctions, lots, and promotions",
                "configured": True,
                "what_it_does": (
                    "Controls what regular (non-admin) users can do and which optional pages appear. "
                    "The snippet below shows your current values."
                    "<ul class='mb-0'>"
                    "<li><code>ALLOW_USERS_TO_CREATE_AUCTIONS</code> &mdash; let users create club auctions. "
                    "False = Django admins only.</li>"
                    "<li><code>ALLOW_USERS_TO_CREATE_LOTS</code> &mdash; let newly created users create standalone lots "
                    "(not attached to an auction). Everyone can still add lots to club auctions.</li>"
                    "<li><code>USERS_ARE_TRUSTED_BY_DEFAULT</code> &mdash; trusted users can promote auctions, manage "
                    "payments, and send invoice notifications. Keep this False so an admin vets accounts first. A common "
                    "setup is <code>ALLOW_USERS_TO_CREATE_AUCTIONS=True</code> with <code>USERS_ARE_TRUSTED_BY_DEFAULT=False</code>.</li>"
                    "<li><code>UNTRUSTED_MESSAGE</code> &mdash; the message untrusted users see when they try to promote an auction.</li>"
                    "<li><code>ENABLE_PROMO_PAGE</code> &mdash; True shows a marketing landing page instead of the "
                    "auctions list as the home page.</li>"
                    "<li><code>ENABLE_CLUB_FINDER</code> &mdash; True adds the &ldquo;find a club&rdquo; map to the menu.</li>"
                    "<li><code>ENABLE_HELP</code> &mdash; True shows the in-auction help button and auction.fish tutorial videos.</li>"
                    "</ul>"
                ),
                "snippets": [
                    {
                        "code": (
                            f'ALLOW_USERS_TO_CREATE_AUCTIONS="{self._yes_no(settings.ALLOW_USERS_TO_CREATE_AUCTIONS)}"\n'
                            f'ALLOW_USERS_TO_CREATE_LOTS="{self._yes_no(settings.ALLOW_USERS_TO_CREATE_LOTS)}"\n'
                            f'USERS_ARE_TRUSTED_BY_DEFAULT="{self._yes_no(settings.USERS_ARE_TRUSTED_BY_DEFAULT)}"\n'
                            'UNTRUSTED_MESSAGE="You cannot currently promote auctions. Please contact the website administrator for access."\n'
                            f'ENABLE_PROMO_PAGE="{self._yes_no(settings.ENABLE_PROMO_PAGE)}"\n'
                            f'ENABLE_CLUB_FINDER="{self._yes_no(settings.ENABLE_CLUB_FINDER)}"\n'
                            f'ENABLE_HELP="{self._yes_no(settings.ENABLE_HELP)}"'
                        )
                    },
                    {
                        "label": (
                            "ALLOW_USERS_TO_CREATE_LOTS only affects new accounts. To toggle standalone-lot creation "
                            "for existing users, run (use off to disable):"
                        ),
                        "code": "docker exec -it django python3 manage.py change_standalone_lots on",
                    },
                ],
            },
            {
                "section": "Core setup",
                "name": "Email delivery",
                "configured": email_configured,
                "what_it_does": "Sends sign-in, invoice, and notification emails. Pick one of the two options below.",
                "snippets": [
                    {
                        "label": "Option A — Gmail (simplest). Turn on 2-step verification first, then create an app password.",
                        "code": (
                            'POST_OFFICE_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend"\n'
                            'EMAIL_HOST="smtp.gmail.com"\n'
                            'EMAIL_PORT="587"\n'
                            'EMAIL_USE_TLS="True"\n'
                            'EMAIL_HOST_USER="you@gmail.com"\n'
                            'EMAIL_HOST_PASSWORD="your-16-character-app-password"\n'
                            'DEFAULT_FROM_EMAIL="Notifications <you@gmail.com>"'
                        ),
                    },
                    {
                        "label": (
                            "Option B — Amazon SES (recommended for production; also enables reply routing). "
                            "INBOUND_ROUTING_SECRET is a random secret you make up — it is this app's own value, not from AWS. "
                            "With SES, mail is sent from info@your-domain automatically (DEFAULT_FROM_EMAIL is ignored)."
                        ),
                        "code": (
                            'POST_OFFICE_EMAIL_BACKEND="django_ses.SESBackend"\n'
                            'AWS_ACCESS_KEY_ID="your-access-key"\n'
                            'AWS_SECRET_ACCESS_KEY="your-secret-key"\n'
                            'AWS_SES_REGION_NAME="us-east-1"\n'
                            'AWS_SES_REGION_ENDPOINT="email.us-east-1.amazonaws.com"\n'
                            'AWS_SES_CONFIGURATION_SET="your-configuration-set"\n'
                            'INBOUND_ROUTING_SECRET="a-long-random-secret"'
                        ),
                    },
                ],
                "links": [
                    {
                        "label": "Gmail: enable 2-step verification",
                        "url": "https://myaccount.google.com/signinoptions/two-step-verification",
                    },
                    {"label": "Gmail: create an app password", "url": "https://myaccount.google.com/apppasswords"},
                    {
                        "label": "Full SES setup guide (SES.md)",
                        "url": "https://github.com/iragm/fishauctions/blob/master/SES.md",
                    },
                ],
            },
            {
                "section": "Core setup",
                "name": "Terms of service",
                "configured": Path(settings.BASE_DIR / "tos.html").exists(),
                "what_it_does": (
                    "Your site needs a terms-of-service page at <code>/tos/</code>. Create a <code>tos.html</code> file "
                    "in the project root (next to your <code>.env</code>) with your terms."
                ),
                "snippets": [
                    {
                        "label": "Create the file in the project root and paste in your terms:",
                        "code": "nano tos.html",
                    }
                ],
            },
            # -- PayPal -----------------------------------------------------------
            {
                "section": "PayPal",
                "name": "PayPal",
                "hide_title": True,
                "configured": env_has_real_value(settings.PAYPAL_CLIENT_ID)
                and env_has_real_value(settings.PAYPAL_SECRET),
                "what_it_does": (
                    "Lets your club collect online payments via PayPal (use Square instead unless you have PayPal "
                    "platform-seller approval)."
                ),
                "where_to_get_it": (
                    "Create a Live REST app under <code>Apps &amp; Credentials</code>, copy its client ID and secret, then "
                    "set the return/webhook URLs below."
                ),
                "snippets": [
                    {"code": 'PAYPAL_CLIENT_ID="your-client-id"\nPAYPAL_SECRET="your-secret"'},
                    {
                        "label": "URLs to configure in your PayPal app",
                        "code": (
                            f"# Return URL:\n{base_url}/paypal/onboard/success/\n"
                            f"# Webhook URL (subscribe to order & payment events):\n{base_url}/paypal/webhook/"
                        ),
                    },
                    {
                        "label": (
                            "To let users connect their own PayPal accounts you need an approved platform partner BN "
                            "code from PayPal, then add these (PAYPAL_ENABLED_FOR_USERS defaults to False, which hides "
                            "the connect-PayPal button for new accounts):"
                        ),
                        "code": (
                            'PARTNER_MERCHANT_ID="your-partner-merchant-id"\n'
                            'PAYPAL_BN_CODE="your-bn-code"\n'
                            'PAYPAL_ENABLED_FOR_USERS="True"'
                        ),
                    },
                    {
                        "label": "Once your integration is tested, enable PayPal for existing accounts:",
                        "code": "docker exec -it django python3 manage.py change_paypal on",
                    },
                    {
                        "label": (
                            "To test against the PayPal sandbox, point the API at it (if unset, sandbox is used in "
                            "development and live in production):"
                        ),
                        "code": 'PAYPAL_API_BASE="https://api-m.sandbox.paypal.com"',
                    },
                ],
                "links": [
                    {
                        "label": "PayPal developer dashboard (get API keys)",
                        "url": "https://developer.paypal.com/dashboard/applications/live",
                    },
                ],
            },
            # -- Square -----------------------------------------------------------
            {
                "section": "Square",
                "name": "Square",
                "hide_title": True,
                "configured": env_has_real_value(settings.SQUARE_APPLICATION_ID)
                and env_has_real_value(settings.SQUARE_CLIENT_SECRET),
                "what_it_does": (
                    "Lets sellers connect Square accounts to collect online payments &mdash; the recommended option. "
                    "Set <code>SQUARE_ENVIRONMENT=production</code> for live payments (blank uses the sandbox)."
                ),
                "where_to_get_it": (
                    "Create an app, then copy the Application ID, OAuth secret, and webhook signature key, and set the "
                    "redirect/webhook URLs below."
                ),
                "snippets": [
                    {
                        "code": (
                            'SQUARE_ENABLED_FOR_USERS="True"\n'
                            'SQUARE_ENVIRONMENT="production"\n'
                            'SQUARE_APPLICATION_ID="sq0idp-xxxxx"\n'
                            'SQUARE_CLIENT_SECRET="sq0csp-xxxxx"\n'
                            'SQUARE_WEBHOOK_SIGNATURE_KEY="your-webhook-key"\n'
                            'FIELD_ENCRYPTION_KEY="your-fernet-key"'
                        )
                    },
                    {
                        "label": "URLs to configure in your Square app",
                        "code": (
                            f"# OAuth redirect URL:\n{base_url}/square/onboard/success/\n"
                            f"# Webhook subscription URL:\n{base_url}/square/webhook/"
                        ),
                    },
                    {
                        "label": "To enable Square for existing users (new users follow SQUARE_ENABLED_FOR_USERS):",
                        "code": "docker exec -it django python3 manage.py change_square on",
                    },
                ],
                "links": [
                    {
                        "label": "Square developer dashboard (get API keys)",
                        "url": "https://developer.squareup.com/apps",
                    },
                ],
            },
            # -- Google Maps ------------------------------------------------------
            {
                "section": "Google Maps",
                "name": "Google Maps",
                "hide_title": True,
                "configured": getattr(settings, "GOOGLE_MAPS_ENABLED", False),
                "what_it_does": "Enables maps on auction and club pages plus location pickers.",
                "where_to_get_it": (
                    "Enable the <code>Maps JavaScript API</code> in Google Cloud and create the key(s) below."
                ),
                "snippets": [
                    {
                        "label": "Browser key — shows the maps and location pickers. Restrict it to your domain (include your port if it isn't 80).",
                        "code": 'GOOGLE_MAPS_API_KEY="your-browser-key"',
                    },
                    {
                        "label": "Server key (optional) — only needed to geocode club member addresses onto the member map. Restrict it to the Geocoding API.",
                        "code": 'GOOGLE_MAPS_SERVER_API_KEY="your-server-key"',
                    },
                ],
                "links": [
                    {
                        "label": "Google Cloud — Maps API keys",
                        "url": "https://console.cloud.google.com/google/maps-apis/credentials",
                    },
                ],
            },
            # -- Google sign-in ---------------------------------------------------
            {
                "section": "Google sign-in",
                "name": "Google sign-in on the website",
                "configured": env_has_real_value(settings.GOOGLE_OAUTH_LINK),
                "what_it_does": (
                    "Adds one-click Google sign-in to the website. The button stays hidden until a real "
                    "client ID is set, so a placeholder degrades gracefully."
                ),
                "where_to_get_it": (
                    "Create an OAuth web application and copy its client ID (ends in "
                    "<code>.apps.googleusercontent.com</code>). Add your site to the authorized origins."
                ),
                "snippets": [{"code": 'GOOGLE_OAUTH_LINK="your-client-id.apps.googleusercontent.com"'}],
                "links": [
                    {
                        "label": "Google Cloud — OAuth credentials",
                        "url": "https://console.cloud.google.com/apis/credentials",
                    },
                    {
                        "label": "Setup guide (django-allauth)",
                        "url": "https://docs.allauth.org/en/latest/socialaccount/providers/google.html",
                    },
                ],
            },
            {
                "section": "Google sign-in",
                "name": "Google sign-in in the mobile app",
                "configured": env_has_real_value(settings.GOOGLE_OAUTH_CLIENT_ID),
                "what_it_does": (
                    "Adds &ldquo;Continue with Google&rdquo; to the mobile app's login screen (separate from the "
                    "website button, since Google blocks OAuth in WebViews)."
                ),
                "where_to_get_it": (
                    "Reuse the website's Web-application OAuth client ID, and create an Android OAuth client for each app "
                    "package name (no secret, just has to exist in the project)."
                ),
                "snippets": [{"code": 'GOOGLE_OAUTH_CLIENT_ID="your-client-id.apps.googleusercontent.com"'}],
                "links": [
                    {
                        "label": "Google Cloud — OAuth credentials",
                        "url": "https://console.cloud.google.com/apis/credentials",
                    },
                ],
            },
            # -- Sign in with Apple -----------------------------------------------
            *self._apple_sign_in_items(base_url, site_host),
            # -- Facebook Login ---------------------------------------------------
            *self._facebook_login_items(base_url),
            # -- Tap to Pay on iPhone ---------------------------------------------
            *self._tap_to_pay_items(),
            # -- Mobile push notifications ---------------------------------------
            {
                "section": "Mobile push notifications",
                "name": "Mobile push notifications (Firebase)",
                "hide_title": True,
                # The service-account key is what actually enables sending; without it every
                # notification falls back to email. The two client files let the app register to
                # receive — flagged in the help text below.
                "configured": bool(getattr(settings, "FIREBASE_CREDENTIALS_JSON", "")),
                "what_it_does": (
                    "Sends push notifications to the mobile app (invoices, watched lots, chat, and more) "
                    "instead of email for users who opt in. Without it those notifications simply fall back "
                    "to email. Three pieces from one Firebase project:"
                    "<ul class='mb-0'>"
                    "<li><code>FIREBASE_CREDENTIALS_JSON</code> &mdash; the <strong>service-account</strong> key "
                    "(a secret) the server uses to send. Inline JSON, or a path to the file.</li>"
                    "<li><code>FIREBASE_ANDROID_CONFIG_FILE</code> &mdash; path to <code>google-services.json</code>, "
                    "the Android app's <em>public</em> config, served to the app so it can register for push.</li>"
                    "<li><code>FIREBASE_IOS_CONFIG_FILE</code> &mdash; path to <code>GoogleService-Info.plist</code>, "
                    "the iOS app's <em>public</em> config.</li>"
                    "</ul>"
                    "The two client files hold only public values; keep the service-account key secret. The "
                    "client files are re-read at startup, so restart after changing them."
                ),
                "where_to_get_it": (
                    "In the Firebase console, create a project (or reuse one) and add an Android app and an iOS "
                    "app to it. Download <code>google-services.json</code> (Android) and "
                    "<code>GoogleService-Info.plist</code> (iOS) from each app's settings. For the server key, "
                    "open <strong>Project settings &rarr; Service accounts</strong> and generate a new private key."
                ),
                "setup_steps": [
                    "Create a Firebase project and add your Android and iOS apps to it.",
                    "Download each app's config file and place them on the server (e.g. a mounted config directory).",
                    "Generate a service-account private key under <strong>Project settings &rarr; Service accounts</strong>.",
                    "Set the three variables below, then restart so the client config files are re-read.",
                ],
                "snippets": [
                    {
                        "code": (
                            'FIREBASE_CREDENTIALS_JSON="/config/firebase-service-account.json"\n'
                            'FIREBASE_ANDROID_CONFIG_FILE="/config/google-services.json"\n'
                            'FIREBASE_IOS_CONFIG_FILE="/config/GoogleService-Info.plist"'
                        )
                    }
                ],
                "links": [
                    {"label": "Firebase console", "url": "https://console.firebase.google.com/"},
                ],
            },
            # -- reCAPTCHA --------------------------------------------------------
            {
                "section": "reCAPTCHA",
                "name": "reCAPTCHA",
                "hide_title": True,
                "configured": getattr(settings, "RECAPTCHA_ENABLED", False),
                "what_it_does": "Protects signup and password reset forms from abuse.",
                "where_to_get_it": (
                    "Register a site of type <strong>reCAPTCHA v2 &ldquo;Invisible&rdquo;</strong> (other types won't "
                    "work here) and copy the site key and secret key."
                ),
                "snippets": [{"code": 'RECAPTCHA_PUBLIC_KEY="your-site-key"\nRECAPTCHA_PRIVATE_KEY="your-secret-key"'}],
                "links": [
                    {"label": "reCAPTCHA admin console (get keys)", "url": "https://www.google.com/recaptcha/admin"},
                ],
            },
            # -- Analytics & ads --------------------------------------------------
            {
                "section": "Analytics & ads",
                "name": "Analytics & ads",
                "hide_title": True,
                "configured": env_has_real_value(settings.GOOGLE_MEASUREMENT_ID)
                or env_has_real_value(settings.GOOGLE_TAG_ID)
                or env_has_real_value(settings.GOOGLE_ADSENSE_ID),
                "what_it_does": (
                    "Optional Google Analytics (<code>GOOGLE_MEASUREMENT_ID</code>), Tag Manager "
                    "(<code>GOOGLE_TAG_ID</code>), and AdSense (<code>GOOGLE_ADSENSE_ID</code>); "
                    "<code>SHOW_ADS</code> is the master ad on/off switch."
                ),
                "snippets": [
                    {
                        "code": (
                            'GOOGLE_MEASUREMENT_ID="G-XXXXXXXXXX"\n'
                            'GOOGLE_TAG_ID="GTM-XXXXXXX"\n'
                            'GOOGLE_ADSENSE_ID="ca-pub-XXXXXXXXXXXXXXXX"\n'
                            f'SHOW_ADS="{self._yes_no(settings.SHOW_ADS)}"'
                        )
                    }
                ],
                "links": [
                    {"label": "Google Analytics", "url": "https://analytics.google.com/"},
                    {"label": "Google Tag Manager", "url": "https://tagmanager.google.com/"},
                    {"label": "Google AdSense", "url": "https://www.google.com/adsense/"},
                ],
            },
            # -- Natural-language command palette ---------------------------------
            {
                "section": "Command palette assistant",
                "name": "Command palette assistant",
                "hide_title": True,
                "configured": llm_configured,
                "what_it_does": (
                    "Lets people type or say what they want in the command palette "
                    "(<kbd>Ctrl</kbd>/<kbd>&#8984;</kbd>+<kbd>K</kbd>) instead of searching for the page: "
                    "&ldquo;add a lot of blue shrimp&rdquo;, &ldquo;check in bob&rdquo;, &ldquo;lot 101 sold to "
                    "bidder 14 for 25&rdquo;. Anything that writes to the database shows a five second countdown "
                    "with a cancel button first, and every action re-runs the same permission checks the web page "
                    "does.<br><br>"
                    "<strong>This is the only setting here that costs money per use.</strong> Each request is one "
                    "or more calls to the model you configure, billed by that provider. The palette throttles to "
                    f"{llm_window_max} calls per {llm_window_minutes} minutes per user, and "
                    "<a href='" + reverse("command_palette_analytics") + "'>command palette analytics</a> shows "
                    "the running token total, what it's being used for, and the queries it couldn't answer.<br><br>"
                    "Leave <code>OPENAI_API_KEY</code> empty and the palette behaves exactly as it did before this "
                    "feature existed: ordinary search, no microphone button, no model calls."
                ),
                "where_to_get_it": (
                    "An API key from your model provider. <code>LLM_MODEL</code> is a one-line model swap and "
                    "<code>LLM_BASE_URL</code> points at any OpenAI-compatible endpoint &mdash; a proxy, "
                    "OpenRouter, or a model running on your own hardware &mdash; so the key doesn't have to be "
                    "OpenAI's. The default model is a small, cheap one; a smarter model gets more commands right "
                    "and costs more per use."
                ),
                "setup_steps": [
                    "Create an API key with your provider and paste it into <code>OPENAI_API_KEY</code>.",
                    "Restart the site, then press <kbd>Ctrl</kbd>/<kbd>&#8984;</kbd>+<kbd>K</kbd> and type "
                    "something like &ldquo;take me to my invoice&rdquo; to check it responds.",
                    "Watch the analytics page for the first few days &mdash; the token total tells you what this "
                    "is actually costing, and the &ldquo;couldn't answer&rdquo; list tells you what people expected "
                    "it to do.",
                ],
                "snippets": [
                    {
                        "code": (
                            'LLM_PROVIDER="openai"\n'
                            f'LLM_MODEL="{settings.LLM_MODEL or "gpt-5-nano"}"\n'
                            'OPENAI_API_KEY="sk-..."\n'
                            'LLM_BASE_URL=""'
                        )
                    }
                ],
                "links": [
                    {"label": "OpenAI API keys", "url": "https://platform.openai.com/api-keys"},
                    {"label": "Command palette analytics", "url": reverse("command_palette_analytics")},
                ],
            },
            # -- Mailchimp --------------------------------------------------------
            {
                "section": "Mailchimp",
                "name": "Mailchimp",
                "hide_title": True,
                "configured": env_has_real_value(settings.MAILCHIMP_CLIENT_ID)
                and env_has_real_value(settings.MAILCHIMP_CLIENT_SECRET),
                "what_it_does": (
                    "Lets clubs connect a Mailchimp account to sync members to an audience. "
                    "Each club connects its own account from its club settings page once these keys are set."
                ),
                "where_to_get_it": "Register an OAuth2 app and copy its client ID and secret.",
                "snippets": [
                    {"code": 'MAILCHIMP_CLIENT_ID="your-client-id"\nMAILCHIMP_CLIENT_SECRET="your-client-secret"'}
                ],
                "links": [
                    {"label": "Register a Mailchimp OAuth app", "url": "https://admin.mailchimp.com/account/oauth2/"},
                ],
            },
            # -- Google Calendar --------------------------------------------------
            {
                "section": "Google Calendar",
                "name": "Google Calendar",
                "hide_title": True,
                "configured": env_has_real_value(settings.GOOGLE_CALENDAR_CLIENT_ID)
                and env_has_real_value(settings.GOOGLE_CALENDAR_CLIENT_SECRET),
                "what_it_does": (
                    "Lets clubs connect a Google account so their auctions and events sync to a shared "
                    "Google calendar, both ways. Each club connects its own account from its club settings "
                    "page once these keys are set. Club event lists and the iCal feed work without this."
                ),
                "where_to_get_it": (
                    "Enable the Google Calendar API, then create an OAuth 2.0 <strong>Web application</strong> "
                    "client and copy its client ID and secret."
                ),
                "setup_steps": [
                    "In the Google Cloud console, enable the <strong>Google Calendar API</strong> for your project.",
                    "Create an OAuth 2.0 Client ID of type <strong>Web application</strong>.",
                    (
                        "Add <code>"
                        f"{base_url}/google-calendar/callback/</code> as an "
                        "<strong>Authorized redirect URI</strong>."
                    ),
                    (
                        "The default scope is <code>calendar.app.created</code>, which only grants access to "
                        "calendars this site creates — so the app avoids Google's sensitive-scope verification "
                        "review. Leave <code>GOOGLE_CALENDAR_SCOPE</code> blank unless you need more."
                    ),
                ],
                "snippets": [
                    {
                        "code": (
                            'GOOGLE_CALENDAR_CLIENT_ID="your-client-id.apps.googleusercontent.com"\n'
                            'GOOGLE_CALENDAR_CLIENT_SECRET="your-client-secret"'
                        )
                    }
                ],
                "links": [
                    {"label": "Google Cloud credentials", "url": "https://console.cloud.google.com/apis/credentials"},
                    {
                        "label": "Enable the Calendar API",
                        "url": "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com",
                    },
                ],
            },
            # -- Discord ----------------------------------------------------------
            {
                "section": "Discord bot",
                "name": "Discord bot",
                "hide_title": True,
                "configured": env_has_real_value(settings.DISCORD_BOT_TOKEN)
                and env_has_real_value(getattr(settings, "DISCORD_PUBLIC_KEY", ""))
                and env_has_real_value(getattr(settings, "DISCORD_BOT_CLIENT_ID", "")),
                "what_it_does": (
                    "Lets members join a Discord server and automatically receive a role and club membership. "
                    "The server keys are set here; each club then links its server from its Discord settings page. "
                    "Discord needs a public HTTPS URL, so this won't work on localhost."
                ),
                "where_to_get_it": (
                    "Create an application with a bot (enable the <code>Server Members Intent</code>), then copy the "
                    "token, public key, and client ID."
                ),
                "setup_steps": [
                    "At the Discord developer portal, create an application and add a bot.",
                    "Under the bot settings, enable the <strong>Server Members Intent</strong>.",
                    "Copy the bot token, public key, and client ID into the <code>.env</code> snippet below.",
                    (
                        "In the application's <strong>General Information</strong> tab, set the "
                        f"<strong>Interactions Endpoint URL</strong> to <code>{base_url}/discord/interactions/</code>."
                    ),
                    (
                        "When adding the bot to a server it needs <strong>Manage Roles</strong> and <strong>Send "
                        "Messages</strong>, and its role must sit <strong>above</strong> any role it assigns in the "
                        "server's role hierarchy."
                    ),
                    (
                        "Register the <code>/connect</code> slash command once (see the command below). Global "
                        "registration can take up to an hour to propagate."
                    ),
                    (
                        "Each club admin then links a server from <code>/clubs/&lt;slug&gt;/discord/</code>: run "
                        "<code>/connect club_uuid:&lt;uuid&gt;</code> in the channel where the join button should appear, "
                        "then use <strong>Fetch roles</strong> to sync role names and configure paid/unpaid roles and "
                        "BAP/HAP thresholds."
                    ),
                ],
                "snippets": [
                    {
                        "code": (
                            'DISCORD_PUBLIC_KEY="your-application-public-key"\n'
                            'DISCORD_BOT_TOKEN="your-bot-token"\n'
                            'DISCORD_BOT_CLIENT_ID="your-application-client-id"'
                        )
                    },
                    {
                        "label": "Register the /connect slash command (run once after the keys are set):",
                        "code": "docker exec -it django python3 manage.py register_discord_commands",
                    },
                ],
                "links": [
                    {"label": "Discord developer portal", "url": "https://discord.com/developers/applications"},
                ],
            },
            # -- Google Wallet ----------------------------------------------------
            {
                "section": "Google Wallet membership cards",
                "name": "Google Wallet membership cards",
                "hide_title": True,
                "configured": bool(
                    settings.GOOGLE_WALLET_ISSUER_ID
                    and settings.GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL
                    and settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY
                ),
                "what_it_does": (
                    "Adds an &ldquo;Add to Google Wallet&rdquo; button on member cards so members can save their "
                    "membership card to their phone; anyone with a member's UUID link can add it."
                ),
                "where_to_get_it": (
                    "Get a Wallet Issuer ID from the issuer console, then drop the Google Cloud service-account key JSON "
                    "next to your <code>.env</code> and set the two vars below."
                ),
                "snippets": [
                    {"code": 'GOOGLE_WALLET_ISSUER_ID="issuer-id"\nGOOGLE_WALLET_KEYFILE="google-wallet-key.json"'},
                ],
                "links": [
                    {"label": "Google Wallet issuer console", "url": "https://pay.google.com/business/console"},
                ],
            },
            # -- Apple Wallet -----------------------------------------------------
            {
                "section": "Apple Wallet membership cards",
                "name": "Apple Wallet membership cards",
                "hide_title": True,
                "configured": bool(
                    settings.APPLE_WALLET_CERT_FILE
                    and settings.APPLE_WALLET_WWDR_FILE
                    and settings.APPLE_WALLET_PASS_TYPE_IDENTIFIER
                    and settings.APPLE_WALLET_TEAM_IDENTIFIER
                ),
                "what_it_does": (
                    "Adds an &ldquo;Add to Apple Wallet&rdquo; button next to the Google Wallet one; anyone with a "
                    "member's UUID link can add it. Requires a paid Apple Developer account ($99/yr)."
                ),
                "where_to_get_it": (
                    "Create a Pass Type ID and certificate, download the Apple WWDR cert, and drop the <code>.p12</code> "
                    "and <code>.pem</code> next to your <code>.env</code>, then set the vars below."
                ),
                "snippets": [
                    {
                        "code": (
                            'APPLE_WALLET_CERT_FILE="pass-type-id.p12"\n'
                            'APPLE_WALLET_CERT_PASSWORD="your-p12-password"\n'
                            'APPLE_WALLET_WWDR_FILE="AppleWWDRCAG4.pem"\n'
                            'APPLE_WALLET_PASS_TYPE_IDENTIFIER="pass.com.example.membership"\n'
                            'APPLE_WALLET_TEAM_IDENTIFIER="ABCDE12345"\n'
                            'APPLE_WALLET_ORGANIZATION_NAME="Your Club"'
                        )
                    }
                ],
                "links": [
                    {
                        "label": "Apple Developer — Pass Type IDs",
                        "url": "https://developer.apple.com/account/resources/identifiers/list/passTypeId",
                    },
                    {"label": "Apple PKI — WWDR certificate", "url": "https://www.apple.com/certificateauthority/"},
                ],
            },
        ]
        return context
