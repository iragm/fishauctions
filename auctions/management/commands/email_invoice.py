from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from post_office import mail

from auctions.email_routing import email_routing_enabled
from auctions.models import Invoice


class Command(BaseCommand):
    help = "Email the winner to pay up on all invoices"

    def handle(self, *args, **options):
        invoices = Invoice.objects.exclude(status="DRAFT").filter(auction__isnull=False, email_sent=False)
        for invoice in invoices:
            if (
                invoice.auction.created_by.userdata.is_trusted
                and invoice.auction.email_users_when_invoices_ready
                and invoice.auctiontos_user.email
            ):
                email = invoice.auctiontos_user.email
                subject = f"Your invoice for {invoice.label} is ready"
                if invoice.status == "PAID":
                    subject = f"Thanks for being part of {invoice.label}"
                contact_email = invoice.auction.created_by.email
                current_site = Site.objects.get_current()
                send_kwargs = {
                    "sender": invoice.auction.sender_email,
                    "template": "invoice_ready",
                    "context": {
                        "subject": subject,
                        "name": invoice.auctiontos_user.name,
                        "domain": current_site.domain,
                        "location": invoice.location,
                        "invoice": invoice,
                    },
                }
                if not email_routing_enabled():
                    send_kwargs["headers"] = {"Reply-to": contact_email}
                    send_kwargs["context"]["reply_to_email"] = contact_email
                mail.send(email, **send_kwargs)
            invoice.email_sent = True
            invoice.save()
