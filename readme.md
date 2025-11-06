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
[...edit your .env file as needed, make sure to remove the first 4 lines ...]
docker compose --profile "*" build
docker compose up -d
```

**Note on SSL Certificate Errors:** If you encounter SSL certificate verification errors during the Docker build (common in corporate environments with SSL inspection), add `DISABLE_PIP_SSL_VERIFY=1` to your `.env` file before building. This is already configured in the CI environment.

**Note on Network Timeouts:** In some network environments (especially those with SSL inspection or strict firewalls), you may experience connection timeouts when downloading Python packages from PyPI. If you encounter timeout errors during the build, try:
- Building on a different network or using a VPN
- Using a PyPI mirror if your organization provides one
- Increasing your Docker daemon's network timeout settings

You should now be able to access a development site at 127.0.0.1 (Note: unlike most Django projects, you probably won't use port 8000)

One last thing to do is to create an admin.  Back in the shell, enter:
```
docker exec -it django python3 manage.py shell -c "from django.contrib.auth import get_user_model; User=get_user_model(); u=User.objects.create_superuser('admin', 'admin@example.com', 'example'); u.emailaddress_set.create(email=u.email, verified=True, primary=True)"
```
Now, back to your web browser, enter the username `admin` and the password `example`, and you should be good to go.  If not, open an issue here and I'll take a look at it.

#### A few notes on development environments

Development-friendly default values are set for most of the environment, but you may wish to use existing databases or specify secure passwords.

The .env.example doesn't document everything, but it has the most common settings.  For example, if you use port 80 for something else, you could serve up the development site on a different port by editing your .env file to have the line HTTP_PORT=81

For more information on the settings and what they do, see the production section, below.

Some stuff (like Google Maps) won't work without an API key.  When generating your API key, make sure to include the port number you use if it's not port 80, for example, http://127.0.0.1:81 if you use port 81

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

A note on migrations: occasionally webpush seems to give permission denied about a file `0006_alter_subscriptioninfo_user_agent.py`.  If this happens, just run `docker exec -u root -it django python3 manage.py makemigrations`

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
* For Payments, create a [PayPal App](https://developer.paypal.com/dashboard/applications/live) and note the client id and secret.

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

To set up payments for your auctions, note that:
* Only auctions created by a site admin (superuser) will be able process payments with the configuration described below (but see the next point for the one exception).

* To allow anyone to connect their PayPal accounts, you need to get an approved platform partner BN code from PayPal and then configure env settings `PARTNER_MERCHANT_ID`, `PAYPAL_BN_CODE`, and `PAYPAL_ENABLED_FOR_USERS=True`.  A management command exists (`docker exec -it django python3 manage.py change_paypal on`) to activate PayPal for existing accounts once you've tested your integration.

Set `PAYPAL_ENABLED_FOR_USERS=False` (this is the default).  This prevents new accounts from seeing a connect PayPal account button, which as noted above, shouldn't be done unless you've configured the partner API.

Set `PAYPAL_CLIENT_ID="client-id"` and `PAYPAL_SECRET="secret"` to the values you got from the pre setup checklist.

If payments are not working after setting this up, make sure your API keys are for live, not sandbox.  To use the sandbox for testing, add `PAYPAL_API_BASE="https://api-m.sandbox.paypal.com"` to your .env.  Note that if this isn't set, sandbox is used in dev and live is used in production.

A few other settings, and what they do:

`NAVBAR_BRAND` This is what's shown on the top of every page.

`COPYRIGHT_MESSAGE` This is shown at the bottom of every page.  HTML allowed here.

`MAILING_ADDRESS` Your physical mailing address, shown next to the unsubscribe link on promo emails.

`ALLOW_USERS_TO_CREATE_AUCTIONS` Set this to False (case sensitive) to allow only admin users to create club auctions

`ALLOW_USERS_TO_CREATE_LOTS` Set this to False (case sensitive) to disable creating stand-alone lots not associated with any auction for newly created users.  Users will still be able to add lots to club auctions.

Directly related to this is a management command `change_standalone_lots` which can be used to enable/disable this for all existing users.  Example: `docker exec -it django python3 manage.py change_standalone_lots on` will allow users to create their own lots.

`ENABLE_PROMO_PAGE` This should be left at False so the main auctions list is the landing page.

`ENABLE_CLUB_FINDER` This should be left at False, setting it to true will show the clubs map dropdown menu.

`ENABLE_HELP` if True, will show the auction's help button and the auction.fish branded tutorial videos

`I_BRED_THIS_FISH_LABEL` is what's shown to users when checking the "breeder points" checkbox

`WEEKLY_PROMO_MESSAGE` is included in the weekly promotional email.  Plain text only.  Generally, leave this blank.

`WEBSITE_FOCUS` Plural, all-lowercase name of whatever your website is focused around.  For example, "fish", "birds", "items"...

Most of the other options in the .env file are pretty self-explanatory.  Booleans (True or False) are case sensitive.
Save and exit nano, then type:
```
docker compose --profile "*" build
docker compose up
```

#### Configure the URL
Run `./update.sh` this should automatically configure the URL you set up in your .env

#### Configure folder permissions
Static files, logs, and images are bound volumes from the host machine, so Docker is not able to configure permissions on them.

On the host machine, from the same folder as update.sh, run:
```
sudo chown -R 1000:1000 ./mediafiles
sudo chown -R 1000:1000 ./auctions/static
sudo chown -R 1000:1000 ./logs
```

The UID and GID of the Docker user need to match the permissions on the host machine, so if for some reason your volumes have a different owner, you can alternatively change the UID/GID for app in the .env file by changing the `PUID` and `PGID` lines (the default is 1000 for both).

If you haven't configured things properly, you'll see a couple warnings when starting the Django Web container, for example, `User 'app' (UID: 1000, GID: 1000) cannot write to "/home/app/web/mediafiles"`.  These should tell you the exact command to run on the host machine (your server) to fix permissions and get up and running.

#### Add your TOS
Finally, create a file called `tos.html` with your terms of service in the same directory as the .env file.

With a little luck, things worked.  If not, open an issue and provide as much detail as possible.  Don't put your keys in the issue, but do include any logs.  Remember that support is very limited for custom production deployments.  If something isn't talked about in this guide, I'm not really interested in helping with it.

### Post setup:
If you didn't get any errors, shut down the containers with control+c and then restart them in detached mode (`docker compose up -d`).  Create a super user with `docker exec -it django python3 manage.py createsuperuser` and then browse to the website and try to log in with that user (if you don't get a verification email, you can use the steps in the development section above to create a super user with a verified email, but don't forget to change the password!).

Go to the Django admin site (it's in the admin dropdown menu) and update the categories and FAQ articles to suit your tastes.

Note that the Django admin site is barely used in production.  I use it to:
- Add blog posts, categories, and FAQs
- Give users super user permission, and permission to create lots/auctions

Everything else can be done through the UI.

Note: Do not remove the default "Uncategorized" category, it's referenced in several places in the code.  It's fine to remove the other default categories.

### Updates:
Updates can be run by typing `./update.sh` in your VM.

Very rarely following an update I've needed to run `docker compose down && docker compose up -d` instead of just running update, so if the site isn't coming back after an update, give this a try before rolling back to your snapshot.

### Changes and new features:
There's quite a bit of other stuff (ads, google analytics, etc.) that was enabled in the past and has been disabled.  If you want this stuff, make a pull request for it.  If you want something:

* you want to disable the captcha on sign up
* or you can't figure out how to get a Vapid key and you want to disable push notifications
* or you want to disable google ouath
* or you want to pull updates from your own repo

or whatever, **make a pull request**.  Make sure that the default settings keep things as they are.

I will (often) add new features that benefit auction.fish.  I will not add new features that benefit your website and don't help auction.fish.
