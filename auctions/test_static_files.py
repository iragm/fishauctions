"""`/static/`: content-hashed names, and the nginx rule that caches them for a year.

Three things have to agree for a CSS edit to reach a browser on deploy, and none of them fails
loudly on its own: `{% static %}` has to render a hashed name, `collectstatic` has to have written
a file under that name, and `nginx_fishauctions.conf` has to recognise a hashed name so that it --
and only it -- is served `immutable`. Drift in any one of those is silent: the page still loads,
it is just cached for the wrong length of time, which nobody notices until a deploy does not take.
"""

import re
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, override_settings

from fishauctions.static_storage import CacheBustedStaticFilesStorage

#: Every name a template asks `{% static %}` for.
STATIC_TAG = re.compile(r"""\{%\s*static\s+["']([^"']+)["']""")


def _template_static_names():
    names = set()
    for template in (Path(settings.BASE_DIR) / "auctions" / "templates").rglob("*.html"):
        names |= set(STATIC_TAG.findall(template.read_text(errors="replace")))
    return names


def _every_static_file():
    """(finder, name) for every static file the finders can see -- the sources, not the collected
    copies, since those are what `collectstatic` will hash."""
    return list(finders.get_finders() and _walk_finders())


def _walk_finders():
    for finder in finders.get_finders():
        yield from finder.list([])


def _nginx_immutable_pattern():
    """The nginx regex for a hashed filename, as a Python one.

    Read out of the config file rather than repeated here, so that this cannot agree with a
    pattern the running nginx does not have.
    """
    location_pattern = None
    for line in (Path(settings.BASE_DIR) / "nginx_fishauctions.conf").read_text().splitlines():
        location = re.match(r'\s*location\s+~\s+"(.+)"\s*\{', line)
        if location:
            location_pattern = location.group(1)
        if re.match(r"\s*add_header\s+Cache-Control\s+.*immutable", line):
            assert location_pattern, "the immutable Cache-Control is not inside a regex location"
            # nginx (PCRE) spells a named group `(?<name>...)`; Python wants `(?P<name>...)`.
            return re.compile(location_pattern.replace("(?<", "(?P<"))
    nothing_immutable = "nothing in nginx_fishauctions.conf is served immutable any more"
    raise AssertionError(nothing_immutable)


class TemplatesNameRealFilesTests(SimpleTestCase):
    """A `{% static %}` name no finder can find is a 404 in production and nothing at all in dev."""

    def test_every_static_name_in_every_template_is_a_real_file(self):
        names = _template_static_names()
        self.assertGreater(len(names), 20, "the template scan found almost nothing -- check it")
        missing = sorted(name for name in names if finders.find(name) is None)
        self.assertEqual(missing, [], f"templates name static files that do not exist: {missing}")


class HashedNamesReachTheYearLongCacheTests(SimpleTestCase):
    """The nginx pattern has to match the names Django actually writes, and nothing else."""

    def test_nginx_recognises_the_names_django_generates(self):
        pattern = _nginx_immutable_pattern()
        storage = CacheBustedStaticFilesStorage()
        for name in ["css/auction_site.css", "js/vendor/jquery.min.js", "favicon.ico", "fonts/glyphs.woff2"]:
            hashed = storage.hashed_name(name, content=ContentFile(b"the bytes do not matter here"))
            with self.subTest(name=name):
                self.assertIsNotNone(
                    pattern.match(f"/static/{hashed}"),
                    f"nginx would not cache {hashed} for a year",
                )

    def test_no_real_static_file_is_mistaken_for_a_hashed_one(self):
        """A vendored file whose own name happens to carry a 12-hex segment would be cached a year.

        Its contents can change under that name, so it must not match. Every findable static file
        is checked rather than a handful, because the one that breaks this will be a file somebody
        drops in later -- and the failure is silent: a stale asset nobody can flush.
        """
        pattern = _nginx_immutable_pattern()
        looks_hashed = sorted(name for storage_root, name in _every_static_file() if pattern.match(f"/static/{name}"))
        self.assertEqual(
            len(looks_hashed), 0, f"these are not hashed names but nginx would treat them as such: {looks_hashed}"
        )
        self.assertGreater(len(list(_every_static_file())), 100, "the static-file scan found almost nothing")


class MissingManifestEntriesDoNotRaiseTests(SimpleTestCase):
    """The whole reason this storage is a subclass.

    Stock `ManifestStaticFilesStorage` raises on anything it has no manifest entry for, and raising
    here means an unstyled site or a failed deploy.
    """

    def test_a_name_with_no_manifest_entry_answers_with_the_plain_name(self):
        """The test containers never run `collectstatic`, so there is no manifest in them at all."""
        storage = CacheBustedStaticFilesStorage()
        storage.hashed_files = {}
        self.assertEqual(storage.stored_name("css/not_collected_anywhere.css"), "css/not_collected_anywhere.css")

    def test_a_name_resolves_when_collectstatic_has_never_run(self):
        """CI's condition exactly: an empty STATIC_ROOT, so there is no manifest and no file.

        This is the case that matters most, because `DEBUG` is forced off in every test run -- so
        hashing is always *on* in tests and it is the empty `STATIC_ROOT` alone that keeps CI
        rendering plain names. Without the fallback, `base.html` would fail to render there.
        """
        with tempfile.TemporaryDirectory() as empty_static_root:
            with override_settings(STATIC_ROOT=empty_static_root, DEBUG=False):
                storage = CacheBustedStaticFilesStorage()
                self.assertEqual(storage.url("css/auction_site.css"), f"{settings.STATIC_URL}css/auction_site.css")

    def test_a_reference_to_a_file_we_never_vendored_is_left_as_written(self):
        """Our `bootstrap.min.css` ends with a sourceMappingURL naming a `.map` that is not here.

        Stock Django aborts the whole of `collectstatic` over it, which on this deploy means
        `entrypoint.sh` prints an error and the site serves whatever the statics volume still held.
        """
        storage = CacheBustedStaticFilesStorage()
        written = "/*# sourceMappingURL=bootstrap.min.css.map */"
        # Django's own pattern, not one written out here: the override returns `match["matched"]`,
        # so if a future Django renames that group this fails rather than silently passing.
        source_map_pattern = next(
            compiled for compiled, _template in storage._patterns["*.css"] if "sourceMappingURL" in compiled.pattern
        )
        match = source_map_pattern.search(written)
        self.assertIsNotNone(match, "Django's sourceMappingURL pattern no longer matches this line")
        self.assertEqual(storage.url_converter("css/vendor/bootstrap.min.css", {})(match), written)


class DebugSkipsHashingTests(SimpleTestCase):
    @override_settings(DEBUG=True)
    def test_static_urls_are_plain_names_in_dev(self):
        """Django hashes nothing while DEBUG is on, which is what keeps a dev edit visible.

        `override_settings` is doing real work here: a test run has `DEBUG` forced off by
        `setup_test_environment`, so this is the only way to reach the branch a dev server takes.
        """
        self.assertEqual(staticfiles_storage.url("css/auction_site.css"), f"{settings.STATIC_URL}css/auction_site.css")
