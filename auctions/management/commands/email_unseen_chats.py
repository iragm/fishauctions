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

#import csv 
class Command(BaseCommand):
    help = 'Send emails to lot creators about activity on their lots'

    def handle(self, *args, **options):
        users = User.objects.filter(userdata__email_me_when_people_comment_on_my_lots=True, lot__lothistory__seen=False, lot__lothistory__changed_price=False, lot__lothistory__notification_sent=False).distinct()
        current_site = Site.objects.get_current()
        for user in users:
            lots = Lot.objects.exclude(is_deleted=True).filter(user=user, lothistory__seen=False, lothistory__changed_price=False).annotate(
                owner_chats=Count('lothistory', filter=Q(lothistory__seen=False, lothistory__changed_price=False, lothistory__notification_sent=False))
            )
            mail.send(
                user.email,
                template='unread_chat_messages',
                context={
                    'name': user.first_name,
                    'domain': current_site.domain,
                    'lots': lots,
                    'unsubscribe': user.userdata.unsubscribe_link
                    },
            )
        LotHistory.objects.filter(notification_sent = False).update(notification_sent=True)