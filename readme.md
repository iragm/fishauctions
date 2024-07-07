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
This project has now been packaged in Docker, so assuming you have docker installed, you should be able to just:
```
git clone https://github.com/iragm/fishauctions
docker compose build
docker compose up
```
You should now be able to access a development site at 127.0.0.1

#### ENV
Development-friendly default values are set for most of the environment, but you may wish to use existing databases or specify secure passwords.  Simply rename the `.env.example` file to `.env`, edit it as needed, and you should be good to go.

#### Cron jobs
Some cron jobs are used to manage models - these run automatically if you're in production (debug=False), but will need to be run manually in development.  These can be found in the crontab file in the same folder as this readme.

#### Adding packages
New packages can be added to requirements.in (in addition to the standard Django settings file).  A simple `docker-compose build` will trigger pip-compile when building the Docker container and will update all files.