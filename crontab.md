### The following cron jobs need to run ###
#### Setup ####
At a terminal (no need to be root), enter:
```
crontab -e
```
You should now be looking at the cron tab editing screen.  Paste the following at the end of the file:
```
# Set lots as ended and declare a winner
* * * * * cd /home/user/auction_site_production/ && /home/user/auction_site_production/venv/bin/python /home/user/auction_site_production/fishauctions/manage.py endauctions
# Send reminder emails about watched items
*/4 * * * * cd /home/user/auction_site_production/ && /home/user/auction_site_production/venv/bin/python /home/user/auction_site_production/fishauctions/manage.py sendnotifications
# Create invoices
15 * * * * cd /home/user/auction_site_production/ && /home/user/auction_site_production/venv/bin/python /home/user/auction_site_production/fishauctions/manage.py invoice
# Email users about invoices
45 * * * * cd /home/user/auction_site_production/ && /home/user/auction_site_production/venv/bin/python /home/user/auction_site_production/fishauctions/manage.py email_invoice
# Update leaderboard
* 23 * * * cd /home/user/auction_site_production/ && /home/user/auction_site_production/venv/bin/python /home/user/auction_site_production/fishauctions/manage.py update_breederboard
# send email
* * * * * cd /home/user/auction_site_production/ && /home/user/auction_site_production/venv/bin/python /home/user/auction_site_production/fishauctions/manage.py send_queued_mail
# send auction notifications
*/3 * * * * cd /home/user/auction_site_production/ && /home/user/auction_site_production/venv/bin/python /home/user/auction_site_production/fishauctions/manage.py auction_emails

# weekly promo email sent on Friday
00 11 * * 5 cd /home/user/auction_site_production/ && /home/user/auction_site_production/venv/bin/python /home/user/auction_site_production/fishauctions/manage.py weekly_promo     

# Send welcome and print reminder emails
*/4 * * * * cd /home/user/auction_site_production/ && /home/user/auction_site_production/venv/bin/python /home/user/auction_site_production/fishauctions/manage.py auctiontos_notifications

# check for duplicate page views
*/15 * * * * cd /home/user/auction_site_production/ && /home/user/auction_site_production/venv/bin/python /home/user/auction_site_production/fishauctions/manage.py remove_duplicate_views

```