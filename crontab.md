# Set lots as ended and declare a winner
* * * * * cd $AUCTIONS_VENV_ROOT && $AUCTIONS_VENV_ROOT/venv/bin/python $AUCTIONS_VENV_ROOT/fishauctions/manage.py endauctions
# Send reminder emails about watched items
*/4 * * * * cd $AUCTIONS_VENV_ROOT && $AUCTIONS_VENV_ROOT/venv/bin/python $AUCTIONS_VENV_ROOT/fishauctions/manage.py sendnotifications
# Create invoices
*/4 * * * * cd $AUCTIONS_VENV_ROOT && $AUCTIONS_VENV_ROOT/venv/bin/python $AUCTIONS_VENV_ROOT/fishauctions/manage.py invoice
