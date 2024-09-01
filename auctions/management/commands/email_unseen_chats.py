from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import *
from django.core.mail import send_mail
from django.db.models import Count, Case, When, IntegerField, Avg
from django.core.files import File
from datetime import datetime
from post_office import mail
from django.template.loader import get_template
import os
import uuid
from django.contrib.sites.models import Site
from django.db.models import Count, Q


# import csv
class Command(BaseCommand):
    help = "Send emails to lot creators about activity on their lots"

    def handle(self, *args, **options):
        current_site = Site.objects.get_current()
        users = User.objects.filter(
            Q(userdata__email_me_when_people_comment_on_my_lots=True)
            | Q(userdata__email_me_about_new_chat_replies=True)
        )
        for user in users:
            if (
                user.userdata.email_me_when_people_comment_on_my_lots
                and user.userdata.my_lot_subscriptions_count
            ) or (
                user.userdata.email_me_about_new_chat_replies
                and user.userdata.other_lot_subscriptions_count
            ):
                mail.send(
                    user.email,
                    template="unread_chat_messages",
                    context={
                        "name": user.first_name,
                        "domain": current_site.domain,
                        "data": user.userdata,
                        "unsubscribe": user.userdata.unsubscribe_link,
                    },
                )
                user.userdata.mark_all_subscriptions_notified
