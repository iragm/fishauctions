### The following cron jobs need to run ###
#### Setup ####
```
sudo su
# edit this path as appropriate
export AUCTIONS_VENV_ROOT='/home/user/auction_site_production/'
# This should be the name of your venv
export VENV_FOLDER_NAME='venv'
crontab -e
```
You should now be looking at the cron tab editing screen.  Paste the following at the end of the file:
```
# Set lots as ended and declare a winner
* * * * * cd $AUCTIONS_VENV_ROOT && $AUCTIONS_VENV_ROOT/$VENV_FOLDER_NAME/bin/python $AUCTIONS_VENV_ROOT/fishauctions/manage.py endauctions
# Send reminder emails about watched items
*/4 * * * * cd $AUCTIONS_VENV_ROOT && $AUCTIONS_VENV_ROOT/$VENV_FOLDER_NAME/bin/python $AUCTIONS_VENV_ROOT/fishauctions/manage.py sendnotifications
# Create invoices
*/4 * * * * cd $AUCTIONS_VENV_ROOT && $AUCTIONS_VENV_ROOT/$VENV_FOLDER_NAME/bin/python $AUCTIONS_VENV_ROOT/fishauctions/manage.py invoice
# Update leaderboard
* 23 * * * cd $AUCTIONS_VENV_ROOT && $AUCTIONS_VENV_ROOT/$VENV_FOLDER_NAME/bin/python $AUCTIONS_VENV_ROOT/fishauctions/manage.py update_breederboard
```