from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from post_office import mail

from auctions.models import Invoice


class Command(BaseCommand):
    help = "Email the winner to pay up on all invoices"

    def handle(self, *args, **options):
        invoices = Invoice.objects.exclude(status="DRAFT").filter(auction__isnull=False, email_sent=False)
        for invoice in invoices:
            # Skip sending emails if the auction creator is not trusted
            if not invoice.auction.created_by.userdata.is_trusted:
                invoice.email_sent = True
                invoice.save()
                continue
            if invoice.auction.email_users_when_invoices_ready and invoice.auctiontos_user.email:
                email = invoice.auctiontos_user.email
                subject = f"Your invoice for {invoice.label} is ready"
                if invoice.status == "PAID":
                    subject = f"Thanks for being part of {invoice.label}"
                contact_email = invoice.auction.created_by.email
                current_site = Site.objects.get_current()
                mail.send(
                    email,
                    headers={"Reply-to": contact_email},
                    template="invoice_ready",
                    context={
                        "subject": subject,
                        "name": invoice.auctiontos_user.name,
                        "domain": current_site.domain,
                        "location": invoice.location,
                        "invoice": invoice,
                    },
                )
            invoice.email_sent = True
            invoice.save()
