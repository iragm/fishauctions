# Fish club auctions

A free, full featured auction platform:

* Run online or in-person auctions
* Reserve and buy now prices
* Automatic invoicing
* Using a projector, show pictures of lots as they are auctioned off
* Users don't need to create an account for in-person auctions
* Support for Breeder Award Programs/Breeder Participation Programs
* Easily copy lots, rules, and users between auctions
* One click export of data to generate marketing lists
* Different seller and club cuts for club members
* Support for multi-location auctions and mailing of lots
* A recommendation system to find lots in large auctions
* Dozens of stats including web traffic, auctioneer speed, lot sell prices over time, and more to help optimize your next auction
* and more!

What started as a free and open source tool to allow fish clubs to run online auctions during COVID has grown into the best club auction platform available, used by dozens of clubs and thousands of users!  If you are part of a club looking to run an auction, please [visit the site here](https://auction.fish)

If you have a suggestion or are a developer who would like to contribute, read on.

## Features and issues
I'm open to adding new features as they are requested.  Please search for your suggestion in the open issues first.

## Development
This tool is built with Python3, Django, Bootstrap and a bit of JQuery.  Some of the auction admin stuff uses HTMx.

### Getting started (development environment)
```
mkdir ~/auction_site
cd ~/auction_site
sudo apt install python3 python3-pip git
# create a venv for the site to live in
python3 -m venv ./venv
# clone the site
git clone https://github.com/iragm/fishauctions
cd fishauctions
# Install required python packages
pip3 install -r requirements.txt
# Edit the venv to include the necessary environemnt settings
nano ~/auction_site/venv/bin/activate
# paste everything from set_evn_example.sh into the end of the file, then save and close
# activate the venv (this will now apply the environemnt settings added in the last step)
source ./venv/bin/activate
cd fishauctions
python3 manage.py runserver
```

### Cron jobs
Some cron jobs are used to manage models, and should be set up outside python's venv on your server. See crontab.md for more information on setting these up
