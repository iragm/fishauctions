"""Content-hashed names for `/static/`, tolerant of the two things that would break a deploy.

`{% static 'css/auction_site.css' %}` renders `css/auction_site.<12 hex>.css`, so
`nginx_fishauctions.conf` can cache a hashed name for a year and a deploy that edits the file
changes the URL that every browser asks for. Without hashing, static URLs are stable forever and a
deploy has to wait out whatever `Cache-Control` says -- which is why that header was one hour.

Django hashes nothing when `DEBUG` is on, so a dev server with `DEBUG=True` serves plain names and
an edit is visible without collecting anything.

**In a test run that is never the case**, whatever `.env` says: `setup_test_environment` forces
`settings.DEBUG = False`. What decides whether a test sees a hashed name is whether `STATIC_ROOT`
has been collected -- the `django` container's volume has, so tests run through `docker exec` there
see hashed names, and CI's is an empty directory, so the fallback below gives plain ones. Either
way a test that spells out a `/static/` path is fragile: ask `staticfiles_storage.url()` for it.
`auctions/test_static_files.py` is written to hold whichever side it runs on.

Two deviations from stock `ManifestStaticFilesStorage`, both because the stock behaviour is to
raise, and a raise here means an unstyled site or a failed deploy. Neither loosens `manifest_strict`
-- see the first one for why that setting is a trap:

- **A name with no manifest entry answers with the plain name.** `collectstatic` runs at container
  start (`entrypoint.sh`), but the test containers never run it, so `staticfiles.json` is simply
  absent there and strict lookup would fail every template that renders `{% static %}` -- which is
  all of them, via `base.html`. Note that `manifest_strict` stays **True** to get this: with it
  False, Django answers a missing entry by hashing whatever is in `STATIC_ROOT`, and for any file
  whose references post-processing rewrote that is the hash of the *source* bytes rather than of
  the collected ones -- a name that is not on disk, so a 404 instead of the plain file. Raising and
  falling back here is the only version that degrades to "serve it uncached".
- **A reference inside a CSS or JS file that points at a file we do not have is left alone.**
  Post-processing rewrites `url(...)`, `@import` and `sourceMappingURL` to hashed names, and stock
  Django aborts the whole `collectstatic` if one target is missing. Our vendored `bootstrap.min.css`
  ends with a `sourceMappingURL` comment naming a `.map` we never vendored, which is exactly that
  case: harmless (devtools does not find the map either way) but fatal to the collect step.
"""

from django.contrib.staticfiles.storage import ManifestStaticFilesStorage


class CacheBustedStaticFilesStorage(ManifestStaticFilesStorage):
    """`ManifestStaticFilesStorage` that degrades to the un-hashed name instead of raising."""

    def stored_name(self, name):
        try:
            return super().stored_name(name)
        except ValueError:
            # No manifest entry: a test container that never ran collectstatic, a manifest lost
            # while the volume kept its files, or an asset a template names that collectstatic
            # never saw. The plain name is the one thing certain to be on disk.
            return name

    def url_converter(self, name, hashed_files, template=None):
        convert = super().url_converter(name, hashed_files, template)

        def convert_if_the_target_exists(match):
            try:
                return convert(match)
            except ValueError:
                # Leave the reference as written -- the same thing the stock converter does with a
                # URL it decides not to touch. `matched` is the whole matched text in every pattern.
                return match.group("matched")

        return convert_if_the_target_exists
