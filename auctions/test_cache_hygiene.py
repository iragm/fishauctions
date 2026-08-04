"""Guards against tests that clear a cache shared with every other parallel worker.

``manage.py test --parallel`` (what CI runs) gives each worker its own database but not its own
cache — they all point at the one Redis in ``settings.CACHES``, where ``cache.clear()`` is a
``FLUSHDB``. A test class clearing the cache in ``setUp`` therefore empties it out from under
whatever every *other* worker is asserting at that moment. That is how
``test_the_jwks_is_not_refetched_for_every_notification`` came to fail CI with ``2 != 1`` after
passing on its own for months: the JWKS it had just cached was flushed by another worker between
the two notifications, so the second one went back to Apple.

``auctions.test_support.isolated_cache`` is the fix. This makes leaving it off fail the build,
rather than one unlucky run in a hundred somewhere else in the suite.
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase

AUCTIONS_DIR = Path(__file__).resolve().parent
DECORATOR = "isolated_cache"


def find_test_modules(directory):
    """Every test module in ``directory`` — what the Django runner would collect."""
    return sorted(directory.glob("test*.py"))


def _decorator_names(node):
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute):
            yield target.attr
        elif isinstance(target, ast.Name):
            yield target.id


def _isolated_classes(tree):
    """Classes in this module that have a cache of their own, by decorator or by inheritance."""
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    isolated = {node.name for node in classes if DECORATOR in _decorator_names(node)}
    # override_settings is inherited, so a decorated base covers every class built on it. Repeat
    # until nothing new is found, since a subclass may be defined above its own base's subclass.
    while True:
        found = {
            node.name
            for node in classes
            if node.name not in isolated and {base.id for base in node.bases if isinstance(base, ast.Name)} & isolated
        }
        if not found:
            return isolated
        isolated |= found


def _clear_calls(node):
    """Lines calling ``cache.clear()`` directly inside ``node``, not in a class nested in it."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            continue
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "clear"
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == "cache"
        ):
            yield child.lineno
        yield from _clear_calls(child)


def check_source(source):
    """``(line, message)`` for each ``cache.clear()`` that would flush other workers' caches."""
    tree = ast.parse(source)
    isolated = _isolated_classes(tree)
    findings = [(line, "cache.clear() outside a test class") for line in _clear_calls(tree)]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name not in isolated:
            findings += [
                (line, f"{node.name} clears the cache without @{DECORATOR}(...)") for line in _clear_calls(node)
            ]
    return sorted(findings)


class CachesAreNotSharedBetweenWorkersTests(SimpleTestCase):
    def test_no_test_class_clears_a_cache_another_worker_is_using(self):
        findings = []
        for path in find_test_modules(AUCTIONS_DIR):
            findings += [
                f"  {path.name}:{line}: {message}" for line, message in check_source(path.read_text(encoding="utf-8"))
            ]
        if findings:
            report = "\n".join(findings)
            self.fail(
                f"{len(findings)} test(s) clear a cache shared with every other --parallel worker.\n"
                f"Decorate the class with @{DECORATOR}(...) from auctions.test_support:\n{report}"
            )

    def test_the_checker_actually_looks_at_this_app_s_tests(self):
        """A checker pointed at nothing would pass for ever without anyone noticing."""
        found = find_test_modules(AUCTIONS_DIR)
        self.assertGreater(len(found), 10, "expected to find the app's test modules")
        self.assertIn(AUCTIONS_DIR / "test_apple_notifications.py", found)


class CacheHygieneCheckerTests(SimpleTestCase):
    """The checker itself, so a refactor can't quietly turn it into a no-op."""

    UNDECORATED = "class Tests(TestCase):\n    def setUp(self):\n        cache.clear()\n"

    def test_an_unisolated_clear_is_caught(self):
        findings = check_source(self.UNDECORATED)
        self.assertEqual([line for line, _ in findings], [3])
        self.assertIn("Tests", findings[0][1])

    def test_a_decorated_class_is_fine(self):
        self.assertEqual(check_source(f'@isolated_cache("x")\n{self.UNDECORATED}'), [])

    def test_a_subclass_of_a_decorated_class_is_fine(self):
        source = (
            '@isolated_cache("x")\n'
            "class Base(TestCase):\n"
            "    pass\n"
            "class Tests(Base):\n"
            "    def setUp(self):\n"
            "        cache.clear()\n"
        )
        self.assertEqual(check_source(source), [])

    def test_a_clear_outside_any_class_is_caught(self):
        self.assertEqual(check_source("cache.clear()\n"), [(1, "cache.clear() outside a test class")])

    def test_clearing_something_that_is_not_the_cache_is_ignored(self):
        self.assertEqual(check_source("class Tests(TestCase):\n    def setUp(self):\n        registry.clear()\n"), [])
