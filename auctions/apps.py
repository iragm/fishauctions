from django.apps import AppConfig


class AuctionsConfig(AppConfig):
    name = "auctions"

    def ready(self):
        import auctions.signals  # noqa: F401
