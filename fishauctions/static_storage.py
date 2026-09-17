"""Content-hashed names for `/static/`, tolerant of the two things that would break a deploy.

`{% static 'css/auction_site.css' %}` renders `css/auction_site.<12 hex>.css`, so
`nginx_fishauctions.conf` can cache a hashed name for a year and a deploy that edits the file changes
the URL every browser asks for. Django hashes nothing when `DEBUG` is on, so a dev edit is visible
without collecting anything.

**In a test run that is never the case**, whatever `.env` says: `setup_test_environment` forces
`DEBUG = False`. What decides whether a test sees a hashed name is whether `STATIC_ROOT` has been
collected -- the `django` container's has, CI's is empty -- so a test that spells out a `/static/`
path is fragile: ask `staticfiles_storage.url()`.

Two deviations from stock `ManifestStaticFilesStorage`, both because stock raises and a raise here
means an unstyled site or a failed deploy:

- **A name with no manifest entry answers with the plain name.** `collectstatic` runs at container
  start, but the test containers never run it. `manifest_strict` stays **True** to get this: with it
  False, Django hashes whatever is in `STATIC_ROOT`, and for a file whose references were rewritten
  that is the hash of the *source* bytes -- a name that is not on disk, so a 404 rather than the
  plain file.
- **A reference inside a CSS or JS file pointing at a file we do not have is left alone.** Our
  vendored `bootstrap.min.css` ends with a `sourceMappingURL` naming a `.map` we never vendored, and
  stock Django aborts the whole `collectstatic` over it.
"""

from django.contrib.staticfiles.storage import ManifestStaticFilesStorage


class CacheBustedStaticFilesStorage(ManifestStaticFilesStorage):
    """`ManifestStaticFilesStorage` that degrades to the un-hashed name instead of raising."""

    def stored_name(self, name):
        try:
            return super().stored_name(name)
        except ValueError:
            # No manifest entry: a test container that never ran collectstatic, a lost manifest, or
            # an asset a template names that collectstatic never saw. The plain name is the one
            # thing certain to be on disk.
            return name

    def url_converter(self, name, hashed_files, template=None):
        convert = super().url_converter(name, hashed_files, template)

        def convert_if_the_target_exists(match):
            try:
                return convert(match)
            except ValueError:
                # Leave the reference as written, as the stock converter does with a URL it decides
                # not to touch. `matched` is the whole matched text in every pattern.
                return match.group("matched")

        return convert_if_the_target_exists
