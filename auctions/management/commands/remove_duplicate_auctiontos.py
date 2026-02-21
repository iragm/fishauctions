from django.core.management.base import BaseCommand
from django.db.models import Count

from auctions.models import AuctionTOS


class Command(BaseCommand):
    help = "Find AuctionTOS records with the same email in the same auction and merge them, keeping the oldest"

    def handle(self, *args, **options):
        # Find auction+email combinations that have more than one AuctionTOS
        duplicates = (
            AuctionTOS.objects.exclude(email__isnull=True)
            .exclude(email="")
            .values("auction", "email")
            .annotate(count=Count("pk"))
            .filter(count__gt=1)
        )
        total_merged = 0
        for dup in duplicates:
            auction_id = dup["auction"]
            email = dup["email"]
            # Get all matching TOS records, oldest first
            tos_records = AuctionTOS.objects.filter(auction_id=auction_id, email=email).order_by("createdon")
            oldest = tos_records.first()
            newer_records = tos_records.exclude(pk=oldest.pk)
            for newer in newer_records:
                newer_pk = newer.pk
                oldest.merge_duplicate(newer)
                total_merged += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Merged duplicate AuctionTOS pk={newer_pk} ({email}) into pk={oldest.pk} in auction {auction_id}"
                    )
                )
        if total_merged == 0:
            self.stdout.write("No duplicate AuctionTOS records found.")
        else:
            self.stdout.write(self.style.SUCCESS(f"Done. Merged {total_merged} duplicate AuctionTOS record(s)."))
