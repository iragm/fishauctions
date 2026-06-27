# Fish club auctions

A free, full featured auction platform:

* Run online or in-person auctions
* Reserve and buy now prices
* Automatic invoicing
* Integrated payment processing with PayPal and Square
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
I'm open to adding new features that make the site better - large or small.  [Open a new issue](https://github.com/iragm/fishauctions/issues/new) with your suggestions or bug reports.  Please include as much detail as possible.

## Development
### Getting started
This project is packaged in Docker, so assuming you have docker installed, you should be able to just:
```
git clone https://github.com/iragm/fishauctions
cd fishauctions
./update.sh
docker compose --profile "*" build
docker compose up -d
```
You should now be able to access a development site at 127.0.0.1 (Note: unlike most Django projects, you probably won't use port 8000)

Enter the username `admin` and the password `example`, and you should be good to go.

**Setup checklist**: After signing in, open **Admin → Setup Checklist** and follow the steps listed there to enable anything you want

**Demo data**: In development mode (DEBUG=True), demo data loads automatically only when single club mode is off and the database is empty.

**Creating a user**: This is done automatically, but you can create additional users with (admin/example shown below):
```
docker exec -it django python3 manage.py shell -c "from django.contrib.auth import get_user_model; User=get_user_model(); u=User.objects.create_superuser('admin', 'admin@example.com', 'example'); u.emailaddress_set.create(email=u.email, verified=True, primary=True)"
```


#### A few notes on development environments
The .env.example doesn't document everything, but it has the most common settings.  For example, if you use port 80 for something else, you could serve up the development site on a different port by editing your .env file to have the line HTTP_PORT=81

Some stuff (like Google Maps) won't work without an API key.  When generating your API key, make sure to include the port number you use if it's not port 80, for example, http://127.0.0.1:81 if you use port 81

#### Production environment differences
At this time, I'm only aware of one production deployment, but since this is open source, you're welcome to spin up your own competing website.

In keeping with 12 Factor, all configuration is done from the .env file and the rest of the environment is kept extremely similar, but:
- Production uses SWAG to add a cert, dev uses plain old Nginx to serve just http content.
- Production uses Gunicorn with a Uvicorn worker process, development uses Uvicorn with a --reload flag (this is configured in entrypoint.sh)

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

### Deploy the website
Log into your VM and enter the following:
```
git clone https://github.com/iragm/fishauctions
cd fishauctions
./update.sh
```
`./update.sh` now copies `.env.example` if needed, prompts for `SITE_DOMAIN`, generates the app/database/VAPID/encryption secrets, and refuses to let the containers start until that one-time setup has been completed.

After the site starts, sign in as a superuser and open **Admin → Setup Checklist**.  That page points you at the `.env` file, shows which settings and integrations are configured, and gives copy/paste `.env` examples and where-to-get-keys links for Gmail, SES, PayPal, Square, Google Maps, Google sign-in, reCAPTCHA, Mailchimp, Discord, and digital membership cards.

Work through that page rather than hand-editing settings from this guide: every `.env` setting and integration has its own item there with step-by-step instructions, the exact lines to add, and the management commands to turn a feature on for users who already exist.

The rest of this section covers the few host-level steps the in-app checklist can't do for you.

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
