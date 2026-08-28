"""The source code reader: what it can serve, and -- much more to the point -- what it cannot.

``read_source`` is the only tool on ``/mcp/`` that reaches outside this site's own database, and on
this deployment the source it reads is bind-mounted into the container next to ``.env``, a Google
Wallet keyfile and the logs. Nothing in :mod:`auctions.source_code` touches a filesystem path, and
the tests that matter most in this file are the ones that check that: the published repository is
fetched whole and read in memory, and every path is resolved against what was in it -- so what the
tool can hand back is exactly what is already on a public web page.
"""

import io
import tarfile
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from auctions import palette_actions, source_code
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase

#: A stand-in repository, small enough to reason about. ``.env`` is deliberately absent, because it
#: is absent from the real one: it is gitignored, so it is not in the archive, so it does not exist
#: as far as this module is concerned -- which is the whole security argument in one missing row.
FAKE_FILES = {
    "README.md": "# Fish\nRecommended reading.\n",
    "auctions/models.py": "\n".join(f"line {number}" for number in range(1, 51)),
    "auctions/species_matching.py": "def suggest_species(name):\n    return []\n",
    "auctions/recommendations.py": "def recommended_lots(user):\n    # the recommendation engine\n    return []\n",
    "auctions/mcp/tools.py": "def descriptor(action):\n    return {}\n",
    "auctions/test_recommendations.py": "def test_recommended_lots():\n    pass\n",
    "docs/design.md": "The recommendation engine is described here.\n",
    "Dockerfile": "RUN apt-get install --no-install-recommends thing\n",
    "auctions/static/logo.png": "\x89PNG-not-really",
}


def _tarball(files=None) -> bytes:
    """The archive GitHub serves: every path inside one top-level directory named for the ref."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, body in (files or FAKE_FILES).items():
            data = body.encode("utf-8")
            info = tarfile.TarInfo(name=f"fishauctions-master/{path}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code


def fake_get(url, **kwargs):
    return FakeResponse(content=_tarball())


@isolated_cache("source-code-base")
class SourceTestCase(SimpleTestCase):
    """The archive is cached in two places -- Django's cache and a per-process memo. Clear both.

    The cache is this class's own (`isolated_cache`, inherited by every subclass) because
    `cache.clear()` on the shared Redis is a FLUSHDB every other --parallel worker feels.
    """

    def setUp(self):
        cache.clear()
        source_code.forget()
        self.addCleanup(source_code.forget)


@override_settings(SOURCE_CODE_URL="https://github.com/iragm/fishauctions", SOURCE_CODE_BRANCH="master")
class RepositorySettingTests(SimpleTestCase):
    def test_the_owner_and_repo_come_off_the_url(self):
        self.assertEqual(source_code.repository(), ("iragm", "fishauctions"))
        self.assertTrue(source_code.configured())

    @override_settings(SOURCE_CODE_URL="")
    def test_blank_turns_the_whole_thing_off(self):
        self.assertIsNone(source_code.repository())
        self.assertFalse(source_code.configured())

    @override_settings(SOURCE_CODE_URL="https://gitlab.example.com/someone/something")
    def test_somewhere_that_is_not_github_is_not_understood(self):
        """A fork hosted elsewhere loses a feature it never had, rather than getting a broken one."""
        self.assertIsNone(source_code.repository())

    @override_settings(SOURCE_CODE_URL="https://github.com/iragm/fishauctions.git")
    def test_a_clone_url_works_too(self):
        self.assertEqual(source_code.repository(), ("iragm", "fishauctions"))

    def test_a_file_links_to_the_lines_it_quoted(self):
        url = source_code.blob_url("auctions/models.py", 10, 20)
        self.assertEqual(url, "https://github.com/iragm/fishauctions/blob/master/auctions/models.py#L10-L20")

    def test_a_path_is_normalized_but_never_resolved(self):
        self.assertEqual(source_code.normalize("/auctions/models.py"), "auctions/models.py")
        self.assertEqual(source_code.normalize("./auctions/"), "auctions")
        # Left exactly as it was: nothing here walks a filesystem, so there is nothing to resolve
        # it against. It simply is not a path in the repository, which is the answer it gets.
        self.assertEqual(source_code.normalize("../../etc/passwd"), "../../etc/passwd")


@isolated_cache("source-code")
@override_settings(SOURCE_CODE_URL="https://github.com/iragm/fishauctions", SOURCE_CODE_BRANCH="master")
class TheArchiveIsTheAllowlistTests(SourceTestCase):
    """Everything else in the module answers out of what was in the archive, and nothing else."""

    def test_a_path_that_is_not_published_does_not_exist(self):
        with patch("auctions.source_code.requests.get", side_effect=fake_get):
            for path in (".env", "../../etc/passwd", "logs/django.log", "auction-1708296065675.json"):
                self.assertFalse(source_code.exists(path), path)
                with self.assertRaises(ValueError, msg=path):
                    source_code.read(path)

    def test_nothing_outside_the_archive_is_ever_named(self):
        """The manifest is built from the archive alone, so there is nothing else it could name."""
        with patch("auctions.source_code.requests.get", side_effect=fake_get):
            self.assertEqual(set(source_code.tree()), set(FAKE_FILES))

    def test_the_archive_is_downloaded_once_and_then_cached(self):
        with patch("auctions.source_code.requests.get", side_effect=fake_get) as fetched:
            source_code.tree()
            source_code.forget()
            source_code.tree()
        self.assertEqual(fetched.call_count, 1)

    def test_a_network_failure_says_so_rather_than_raising_something_unreadable(self):
        import requests

        with patch("auctions.source_code.requests.get", side_effect=requests.RequestException("down")):
            with self.assertRaises(source_code.SourceUnavailable):
                source_code.tree()

    def test_a_refusal_from_github_is_the_same_kind_of_problem(self):
        with patch("auctions.source_code.requests.get", return_value=FakeResponse(content=b"nope", status_code=404)):
            with self.assertRaises(source_code.SourceUnavailable):
                source_code.tree()

    def test_an_archive_that_will_not_open_is_not_a_crash(self):
        with patch("auctions.source_code.requests.get", return_value=FakeResponse(content=b"not a tarball")):
            with self.assertRaises(source_code.SourceUnavailable):
                source_code.tree()


@isolated_cache("source-code-reads")
@override_settings(SOURCE_CODE_URL="https://github.com/iragm/fishauctions", SOURCE_CODE_BRANCH="master")
class ReadingTests(SourceTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch("auctions.source_code.requests.get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_directory_lists_what_is_directly_inside_it(self):
        inside = source_code.listing("auctions")
        self.assertEqual(inside["directories"], ["mcp", "static"])
        self.assertEqual(
            [row["name"] for row in inside["files"]],
            ["models.py", "recommendations.py", "species_matching.py", "test_recommendations.py"],
        )

    def test_the_top_level_is_a_directory_too(self):
        top = source_code.listing("")
        self.assertEqual(top["directories"], ["auctions", "docs"])
        self.assertEqual([row["name"] for row in top["files"]], ["Dockerfile", "README.md"])

    def test_a_filename_search_puts_the_filename_match_first(self):
        self.assertEqual(source_code.find("models.py")[0], "auctions/models.py")
        self.assertIn("auctions/species_matching.py", source_code.find("species"))

    def test_a_file_comes_back_numbered(self):
        page = source_code.read("auctions/models.py", start=1, count=3)
        self.assertEqual(page["text"], "1\tline 1\n2\tline 2\n3\tline 3")
        self.assertEqual(page["lines"], 50)
        self.assertEqual(page["showing"], "1-3")
        self.assertTrue(page["more"])
        self.assertEqual(page["next_line"], 4)

    def test_a_start_line_past_the_end_does_not_loop_forever(self):
        """Otherwise an agent paging through gets handed the same next_line it was just given."""
        page = source_code.read("auctions/models.py", start=500, count=10)
        self.assertEqual(page["showing"], "nothing")
        self.assertFalse(page["more"])
        self.assertIsNone(page["next_line"])

    def test_the_last_page_says_there_is_no_more(self):
        page = source_code.read("auctions/models.py", start=48, count=50)
        self.assertFalse(page["more"])
        self.assertIsNone(page["next_line"])

    def test_a_page_is_bounded_by_characters_as_well_as_lines(self):
        """The character bound is the one that keeps this from busting the result budget."""
        with patch.object(source_code, "MAX_CHARS", 30):
            page = source_code.read("auctions/models.py", start=1, count=source_code.MAX_LINES)
        self.assertLess(len(page["text"]), 60)
        self.assertTrue(page["more"])

    def test_a_binary_file_is_listed_but_not_read(self):
        self.assertTrue(source_code.exists("auctions/static/logo.png"))
        with self.assertRaises(ValueError):
            source_code.read("auctions/static/logo.png")


@isolated_cache("source-code-grep")
@override_settings(SOURCE_CODE_URL="https://github.com/iragm/fishauctions", SOURCE_CODE_BRANCH="master")
class ContentSearchTests(SourceTestCase):
    """The half the whole archive exists for: "how does X work" is not a filename."""

    def setUp(self):
        super().setUp()
        patcher = patch("auctions.source_code.requests.get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_finds_a_line_of_code(self):
        hits = source_code.grep("recommendation engine")
        self.assertTrue(hits)
        self.assertIn("auctions/recommendations.py", {hit["path"] for hit in hits})

    def test_a_definition_outranks_a_mention(self):
        """ "recommend" is in a Dockerfile flag and four headings; the code has to come first."""
        first = source_code.grep("recommend")[0]
        self.assertEqual(first["path"], "auctions/recommendations.py")
        self.assertIn("def recommended_lots", first["text"])

    def test_tests_and_docker_rank_below_the_application(self):
        ordered = [hit["path"] for hit in source_code.grep("recommend")]
        self.assertLess(ordered.index("auctions/recommendations.py"), ordered.index("docs/design.md"))
        self.assertLess(ordered.index("docs/design.md"), ordered.index("Dockerfile"))
        self.assertLess(ordered.index("Dockerfile"), ordered.index("auctions/test_recommendations.py"))

    def test_one_file_cannot_fill_the_answer(self):
        with patch.object(source_code, "MAX_GREP_PER_FILE", 1):
            hits = source_code.grep("recommend")
        by_file = [hit["path"] for hit in hits]
        self.assertEqual(len(by_file), len(set(by_file)))

    def test_nothing_to_look_for_finds_nothing(self):
        self.assertEqual(source_code.grep(""), [])

    def test_a_line_carries_its_number_so_the_answer_is_checkable(self):
        hit = source_code.grep("suggest_species")[0]
        self.assertEqual(hit["path"], "auctions/species_matching.py")
        self.assertEqual(hit["line"], 1)


@isolated_cache("source-code-tool")
@override_settings(SOURCE_CODE_URL="https://github.com/iragm/fishauctions", SOURCE_CODE_BRANCH="master")
class ReadSourceToolTests(StandardTestCase):
    """The tool on top of it, run the way an agent runs it."""

    def setUp(self):
        super().setUp()
        cache.clear()
        source_code.forget()
        self.addCleanup(source_code.forget)

    def _run(self, params=None):
        request = RequestFactory().post("/")
        request.user = self.user
        request.palette_page = {}
        return palette_actions.run_action(request, "read_source", params or {})

    def test_no_path_lists_the_top_level_and_says_where_to_start(self):
        with patch("auctions.source_code.requests.get", side_effect=fake_get):
            result = self._run({})
        self.assertTrue(result["found"])
        self.assertEqual(result["kind"], "directory")
        self.assertIn("auctions", result["directories"])
        self.assertTrue(result["start_here"])

    def test_a_file_comes_back_with_its_lines(self):
        with patch("auctions.source_code.requests.get", side_effect=fake_get):
            result = self._run({"path": "auctions/models.py", "lines": 2})
        self.assertEqual(result["kind"], "file")
        self.assertIn("1\tline 1", result["text"])
        self.assertIn("start_line=3", result["summary"])

    def test_a_directory_is_told_apart_from_a_file(self):
        with patch("auctions.source_code.requests.get", side_effect=fake_get):
            result = self._run({"path": "auctions/mcp"})
        self.assertEqual(result["kind"], "directory")
        self.assertEqual([row["name"] for row in result["files"]], ["tools.py"])

    def test_a_near_miss_carries_what_it_would_have_found(self):
        with patch("auctions.source_code.requests.get", side_effect=fake_get):
            result = self._run({"path": "auctions/mcp/tool.py"})
        self.assertFalse(result["found"])
        self.assertIn("auctions/mcp/tools.py", result["paths"])

    def test_searching_answers_with_the_code_and_the_filenames(self):
        """The question this tool was asked for: a feature name, not a file name."""
        with patch("auctions.source_code.requests.get", side_effect=fake_get):
            result = self._run({"search": "recommendation engine"})
        self.assertTrue(result["found"])
        self.assertIn("auctions/recommendations.py", result["files_containing_it"])
        self.assertTrue(result["in_the_code"])

    def test_a_search_that_matches_nothing_says_so(self):
        with patch("auctions.source_code.requests.get", side_effect=fake_get):
            result = self._run({"search": "quibblewick"})
        self.assertFalse(result["found"])
        self.assertEqual(result["in_the_code"], [])

    def test_a_secret_beside_the_source_is_simply_not_there(self):
        with patch("auctions.source_code.requests.get", side_effect=fake_get):
            result = self._run({"path": ".env"})
        self.assertFalse(result["found"])
        self.assertNotIn("text", result)

    @override_settings(SOURCE_CODE_URL="")
    def test_a_site_that_publishes_nothing_says_so(self):
        result = self._run({"path": "auctions/models.py"})
        self.assertIn("error", result)

    def test_a_repository_that_cannot_be_reached_points_at_it_instead(self):
        import requests

        with patch("auctions.source_code.requests.get", side_effect=requests.RequestException("down")):
            result = self._run({"path": "auctions/models.py"})
        self.assertIn("github.com/iragm/fishauctions", result["error"])
