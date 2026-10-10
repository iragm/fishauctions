"""Print whether now is a quiet time to deploy production. Advice only; see auctions/deploy_window.py."""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Say whether traffic is near its lows and no auction is in play, so a deploy won't hurt."

    def handle(self, *args, **options):
        from auctions import deploy_window

        window = deploy_window.deploy_window()
        self.stdout.write(deploy_window.summary(window))
        self.stdout.write(
            f"{window['views_last_hour']} page views in the last hour; quiet hours last week saw about "
            f"{window['typical_low_per_hour']}, the busiest {window['busiest_hour_last_week']}."
        )
        for auction in window["auctions_in_play"]:
            self.stdout.write(f"  {auction['title']} ({auction['slug']}): {auction['why']}")
