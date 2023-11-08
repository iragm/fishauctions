from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import *
from django.core.mail import send_mail
from django.db.models import Count, Case, When, IntegerField, Avg
from django.core.files import File
import datetime
from post_office import mail
from django.template.loader import get_template
import os
import uuid
from django.contrib.sites.models import Site
import csv
from auctions.filters import get_recommended_lots

def send_tos_notification(template, tos):
    current_site = Site.objects.get_current()
    mail.send(
        tos.user.email,
        template=template,
        headers={'Reply-to': tos.auction.created_by.email},
        context={
            'domain': current_site.domain,
            'tos': tos,
            }
        )

class Command(BaseCommand):
    help = 'Reminder emails confirming the auction time and location after people join'

    def handle(self, *args, **options):
        # Fixes: https://github.com/iragm/fishauctions/issues/100
        # there are currently two confirmation emails sent.  A field for an additional one (second_confirm_email_sent) exists, but isn't used.
        # we will only send these when people join through the website, not when you manually add them to the auction
        base_qs = AuctionTOS.objects.filter(manually_added=False, user__isnull=False).exclude(pickup_location__pickup_by_mail=True)
        welcome_email_qs = base_qs.filter(confirm_email_sent=False)
        # there's an additional filter to make sure the tos is 24 hours old here -- this is to give a better chance of the user's location being set 
        online_auction_welcome = welcome_email_qs.filter(auction__is_online=True, auction__lot_submission_start_date__lte=timezone.now(), createdon__lte=timezone.now()-datetime.timedelta(hours=24))
        for tos in online_auction_welcome:
            tos.confirm_email_sent = True
            tos.save()
            if not tos.auction.closed: 
                send_tos_notification('online_auction_welcome', tos)
                if tos.closer_location_savings > 9:
                    current_site = Site.objects.get_current()
                    mail.send(
                        tos.auction.created_by.email,
                        template='wrong_location_selected',
                        context={
                            'domain': current_site.domain,
                            'tos': tos,
                            }
                        )
                if tos.trying_to_avoid_ban:
                    current_site = Site.objects.get_current()
                    mail.send(
                        tos.auction.created_by.email,
                        template='user_joined_auction_despite_ban',
                        context={
                            'domain': current_site.domain,
                            'tos': tos,
                            }
                        )
        in_person_auction_welcome = welcome_email_qs.filter(auction__is_online=False, auction__date_start__gte=timezone.now())
        for tos in in_person_auction_welcome:
            tos.confirm_email_sent = True
            tos.save()
            send_tos_notification('in_person_auction_welcome', tos)
        print_reminder_qs = base_qs.filter(print_reminder_email_sent=False)
        online_auction_print_reminder = print_reminder_qs.filter(auction__is_online=True, auction__date_end__lte=timezone.now() - datetime.timedelta(hours=1))
        for tos in online_auction_print_reminder:
            tos.print_reminder_email_sent = True
            tos.save()
            if tos.unbanned_lot_count:
                send_tos_notification('auction_print_reminder', tos)
        in_person_auction_print_reminder = print_reminder_qs.filter(auction__is_online=False, auction__date_start__lte=timezone.now() - datetime.timedelta(hours=28))
        for tos in in_person_auction_print_reminder:
            tos.print_reminder_email_sent = True
            tos.save()
            if tos.unbanned_lot_count:
                send_tos_notification('auction_print_reminder', tos)
        # this is a quick reminder to join auctions that you've viewed but haven't joined.  Fixes #134
        join_auction_reminder = AuctionCampaign.objects.filter(timestamp__lte=timezone.now() - datetime.timedelta(hours=24),
                                                        user__isnull=False,
                                                        email_sent=False,
                                                        auction__isnull=False,
                                                        user__userdata__has_unsubscribed=False)
        for camapign in join_auction_reminder:
            email = camapign.user.email
            lots = Lot.objects.filter(pageview__user=camapign.user, auction=camapign.auction)
            camapign.email_sent = True
            camapign.save()
            send_email = True
            # don't send these emails if it's too late to join, such as an online auction that's ended or an in-person auction that's started
            if camapign.auction.closed:
                send_email = False
            if not camapign.auction.is_online and camapign.auction.started:
                send_email = False
            user_has_already_joined = AuctionTOS.objects.filter(user=camapign.user, auction=camapign.auction).first()
            if user_has_already_joined:
                send_email = False
            if send_email:
                userData, created = UserData.objects.get_or_create(
                    user = camapign.user,
                    defaults={},
                )
                current_site = Site.objects.get_current()
                mail.send(
                    email,
                    template='join_auction_reminder',
                    headers={'Reply-to': camapign.auction.created_by.email},
                    context={
                        'domain': current_site.domain,
                        'auction': camapign.auction,
                        'uuid': camapign.link,
                        'lots': lots,
                        'user':camapign.user,
                        'unsubscribe':userData.unsubscribe_link
                        }
                    )