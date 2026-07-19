from django.apps import AppConfig


class AuctionsConfig(AppConfig):
    name = "auctions"

    def ready(self):
        import auctions.signals  # noqa: F401

        self._require_ads_txt()

    @staticmethod
    def _require_ads_txt():
        """Fail fast if AdSense is configured but the ads.txt template is missing.

        ``ads.txt`` is intentionally gitignored (a per-deployment file), so the
        ``/ads.txt`` route points at a template that only exists if the deployment
        supplied one. When it's absent, every crawler hit raises TemplateDoesNotExist
        -> 500 -> admin email; staging got flooded this way. If a publisher id is set
        we refuse to start until ads.txt exists, rather than 500 under bot traffic.
        Deployments without AdSense (CI, ad-free installs) are unaffected.
        """
        from django.conf import settings

        if not getattr(settings, "GOOGLE_ADSENSE_ID", ""):
            return
        from django.template import TemplateDoesNotExist
        from django.template.loader import get_template

        try:
            get_template("ads.txt")
        except TemplateDoesNotExist:
            from django.core.exceptions import ImproperlyConfigured

            msg = (
                "GOOGLE_ADSENSE_ID is set but the ads.txt template is missing. It is "
                "intentionally gitignored (see .gitignore) and must be provided per "
                "deployment at auctions/templates/ads.txt, or every /ads.txt crawler "
                "request will 500. Create it with your Authorized Digital Sellers entries."
            )
            raise ImproperlyConfigured(msg) from None
