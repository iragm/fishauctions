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

```