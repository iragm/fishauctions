# Fish club auctions

This is a free and open source tool to allow fish clubs to run online auctions during COVID.

If you are part of a club looking to run an auction, please [visit the site here](https://auctions.toxotes.org/about)

If you have a suggestion or are a developer who would like to contribute, read on.

## Features and issues
I'm open to adding new features as they are requested.  Please search for your suggestion in the open issues first

## Development
This tool is built with Python3, Django, Bootstrap and a bit of JQuery.  It's fairly basic, but it does what my club needs it for.

### Getting started (development environment)
```
mkdir ~/auction_site
cd ~/auction_site
sudo apt install python3 pip3 git
# create a venv for the site to live in
python3 -m venv ./venv
# activate the venv
source ./venv/bin/activate
# clone the site
git clone https://github.com/iragm/fishauctions

cd fishauctions
python3 manage.py runserver
```

### Cron jobs
Some cron jobs are used to manage models, and should be set up outside python's venv on your server

See crontab.md for more information on setting these up
