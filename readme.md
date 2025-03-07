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
cd fishauctions
cp .env.example .env
[...edit your .env file as needed...]
docker compose --profile "*" build
docker compose up -d
```
You should now be able to access a development site at 127.0.0.1 (Note: don't use port 8000)

#### ENV
Development-friendly default values are set for most of the environment, but you may wish to use existing databases or specify secure passwords.  Simply rename the `.env.example` file to `.env`, edit it as needed (making sure to remove the lines used for production), and you should be good to go.

This file doesn't document everything, but it has the most common settings.  For example, if you use port 80 for something else, you could serve up the development site on a different port by editing your .env file to have the line HTTP_PORT=81

#### Production environment differences
At this time, I'm only aware of one production deployment, but since this is open source, you're welcome to spin up your own competing website.

In keeping with 12 Factor, all configuration is done from the .env file and the rest of the environment is kept extremely similar, but:
- Production uses SWAG to add a cert, dev uses plain old Nginx to serve just http content.
- Production uses Gunicorn with a Uvicorn worker process, development uses Uvicorn with a --reload flag (this is configured in entrypoint.sh)

#### Cron jobs
Some cron jobs are used to manage models - these run automatically if you're in production (debug=False), but will need to be run manually in development.  These can be found in the crontab file in the same folder as this readme.

#### Adding packages
New packages can be added to requirements.in (in addition to the standard Django settings file) for production dependencies, or requirements-test.in for test dependencies.  Then run `./.github/scripts/update-packages.sh` to generate updated requirements.txt files.

If you would like to upgrade all packages to the latest supported version, run `./.github/scripts/update-packages.sh --upgrade`

### Running tests and lints

To run the same set of tests and lints that would be run in CI, run `docker compose run --rm test --ci`.
This will fail if changes are required. There are additional commands below that can be used to run individual test/lint components and auto-fix issues where possible.

#### Auto-formatting
To format code, run `docker compose run --rm test --format`. This will attempt to auto-fix issues, if possible.

To check if code is formatted properly *without* modifying any files on disk, run `docker compose run --rm test --format-check`. The command will output failures (if any) and exit with an error if changes need to be made.

#### Linting
To lint the code, run `docker compose run --rm test --lint`. This will run a full linting suite and attempt to auto-fix problems.

To check if code passes the linting check *without* modifying any files on disk, run `docker compose run --rm test --lint-check`. The command will output failures (if any) and exit with an error if changes need to be made.

#### Management commands
Run these with docker exec after docker compose is up.  For example: `docker exec -it django python3 manage.py makemigrations`

### Developing in VSCode

This project is optimized for development in [Visual Studio Code](https://code.visualstudio.com/).
If you are using VSCode, begin by installing the ["Remote Development" extension pack](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.vscode-remote-extensionpack), and make sure Docker is also installed.

Working in a VSCode development container will ensure that all project dependencies are installed--aiding in running the Python language server, IDE auto-completion, running tests automatically, etc.

To begin, follow the VS Code prompt to `Reopen in Container`. The first time running this command will take a while as the remote development container is built.
You can also open this project in a remote container by opening the command palette (CMD+Shift+P), and navigate to `Dev Containers: Reopen in Container`.

### Pre-Commit Hook

You can optionally enable a pre-commit hook that will ensure code auto-formatting and linting has run before you commit your change. While not necessary, it can help make sure a commit will pass CI before it is pushed.

To install on your local machine, install via `pip install pre-commit`, then run `pre-commit install` to register this project's pre-commit hooks.

By default, `pre-commit` only runs on files that were changed. When installing `pre-commit` for the first time, you can optionally run `pre-commit run --all-files` to run the hooks against all files.


## Running your own auction website
Support for you running your own auction website is extremely limited (read: non-existent).  That said, here's the basics of getting started.

### Pre setup checklist:
* Register a domain name with your favorite registrar.  I like Cloudflare, it also provides free DDOS protection and caching.
* Purchase a VM.  I use Hostinger.  I don't love them, but they're cheap.  They're fine.  [Grab a KVM2 and use this referral code to get 20% off](https://www.hostinger.com/cart?product=vps%3Avps_kvm_2&period=12&referral_type=cart_link&REFERRALCODE=LXNOIRADNJML&referral_id=01957151-831f-71d8-8b2e-10baf21e9524).  I use and recommend Ubuntu as the OS.
* Get a Vapid key [by following the instructions here](https://pypi.org/project/django-webpush/).  This is for push notifications.
* Get a Gmail address with 2F enabled and get an [app key](https://support.google.com/accounts/answer/185833?hl=en), or sign up for Amazon's SES.
* Get a [Google Maps API key](https://console.cloud.google.com/)
* Get a Google OAUTH key by following the [app registration section here](https://docs.allauth.org/en/latest/socialaccount/providers/google.html).  Just do steps 1 and 2 and make a note of the secret keys, the Django configuration has already been done.
* Get a [Recaptcha v2 invisible key](https://cloud.google.com/security/products/recaptcha)

### Deploy the website
Log into your VM and enter the following:
```
git clone https://github.com/iragm/fishauctions
cd fishauctions
cp .env.example .env
nano .env
```
Go through what's there and enter the keys you got in the pre-setup checklist above.  One thing to pay attention to is the email configuration.
If you chose Gmail, configure things like this:
```
POST_OFFICE_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend"
EMAIL_USE_TLS='True'
EMAIL_HOST='smtp.gmail.com'
EMAIL_PORT='587'
EMAIL_HOST_USER='example@gmail.com'
EMAIL_HOST_PASSWORD='your gmail app password, not your gmail password'
```
If you're using Amazon SES, the above settings won't be used, set these up instead:
```
POST_OFFICE_EMAIL_BACKEND="django_ses.SESBackend"
AWS_ACCESS_KEY_ID="secret"
AWS_SECRET_ACCESS_KEY="secret"
AWS_SESSION_PROFILE="default"
AWS_SES_REGION_NAME="us-east-1"
AWS_SES_REGION_ENDPOINT="email.us-east-1.amazonaws.com"
AWS_SES_CONFIGURATION_SET="secret"
```
Most of the other options in the .env file are pretty self-explanatory.  Booleans (True or False) are case sensitive.
Save and exit nano, then type:
```
docker compose --profile "*" build
docker compose up
```
With a little luck, things worked.  If not, open an issue and provide as much detail as possible.  Don't put your keys in the issue, but do include any logs.  Remember that support is very limited for custom production deployments.  If something isn't talked about in this guide, I'm not really interested in helping with it.

### Post setup:
If you didn't get any errors, shut down the containers with control+c and then restart them in detached mode (`docker compose up -d`).  Create a super user with `docker exec -it django python3 manage.py` and then browse to the website and try to log in with that user.

Log into the admin site and update the categories and FAQ articles to suit your tastes.

### Updates:
Updates can be run by typing `./update.sh` in your VM.  This pulls the latest from iragm/fishauctions right now.

### Changes and new features:
There's quite a bit of other stuff (ads, google analytics, etc.) that was enabled in the past and has been disabled.  If you want this stuff, make a pull request for it.  If you want something:

* you want to disable the captcha on sign up
* or you can't figure out how to get a Vapid key and you want to disable push notifications
* or you want to disable google ouath
* or you want to pull updates from your own repo

or whatever, **make a pull request**.  Make sure that the default settings keep things as they are.

I will (often) add new features that benefit auction.fish.  I will not add new features that benefit your website and don't help auction.fish.
