### The following cron jobs need to run ###
#### Setup ####
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
*/4 * * * * cd /home/user/auction_site_production/ && /home/user/auction_site_production/venv/bin/python /home/user/auction_site_production/fishauctions/manage.py invoice
# Update leaderboard
* 23 * * * cd cd /home/user/auction_site_production/ && /home/user/auction_site_production/venv/bin/python /home/user/auction_site_production/fishauctions/manage.py update_breederboard
```