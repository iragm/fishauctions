from django.utils import timezone
from auctions.models import *
from django.core.management.base import BaseCommand, CommandError
import datetime
import requests

class Command(BaseCommand):
    help = 'Set user lat/long based on their IP address'
    def handle(self, *args, **options):
        # get users that have been on the site for at least 1 days, but have not set their location
        recently = timezone.now() - datetime.timedelta(days=1)
        users = UserData.objects.filter(last_ip_address__isnull=False, latitude=0, longitude=0, user__date_joined__lte=recently).order_by('-last_activity')[:100]
        # build a list of IPs - bit awkward as we can't use single quotes here, and it has to be a string, not a list
        ip_list = "["
        if users:
            for user in users:
                ip_list += f'"{user.last_ip_address}",'
            ip_list = ip_list[:-1] + "]" # trailing , breaks things
            # See here for more documentation: https://ip-api.com/docs/api:batch#test
            r = requests.post('http://ip-api.com/batch?fields=25024', data=ip_list)
            if r.status_code == 200:
                ip_addresses = r.json()
                # now, we cycle through users again and assign their location based on IP
                for user in users:
                    for value in ip_addresses:
                        try:
                            if user.last_ip_address == value['query']:
                                if value['status'] == "success":
                                    user.latitude = value['lat']
                                    user.longitude = value['lon']
                                    user.save()
                                    print(f'assigning {user.user.email} with IP {user.last_ip_address} a location')
                                    break
                                else:
                                    print(f"IP {user.last_ip_address} may not be valid - verify it and set their location manually")
                        except Exception as e:
                            print(e)
            else:
                print("Query failed for this IP list:")
                print(ip_list)
                print(r['text'])
            # some limitations to note:
            # we are capped at 100 lookups per query
            # Looks like the cap on this service is 15 per minute, so it's easily able to meet our needs if the cron job is run more often.
            # right now, this is run once per day via cron.  Not a big deal for current user loads, and this can be run twice a day if needed
            # older users don't have a location assigned (around 440 users, I do not have a way to assign a location to these automatically)
            # if there is a problematic IP, it may be hard to spot, the error checking is minimal here