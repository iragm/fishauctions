"""Guards the module map: that it is current, and that the modules it reads are worth reading.

``docs/module_map.md`` is generated from every module's docstring and top-level names
(:mod:`auctions.module_map`). Checked in, it is the "which file do I open" index an agent or a new
maintainer can read without running anything -- and the moment it is checked in it becomes a file
that can be wrong. These tests are the reason it cannot be: the map is regenerated in memory on
every run and compared to the copy on disk, so a module added, renamed or re-described without
regenerating fails the build rather than sitting there misdirecting people.

One more rule rides along, because it is the same kind of claim: the ``auctions.views`` package
says in its own docstring that its import graph is **acyclic**, and a cycle there is an
``ImportError`` at startup that Django reports against whichever module happened to be imported
first. Written down and not checked, that is exactly the prose this repository does not keep.

The other three tests are the rules that keep the *inputs* honest, and they matter more than the
map does. A generated map of files that do not say what they are for is a list of filenames. So:
anything over 300 lines carries a docstring, nothing new is born over 1500 lines, and the modules
that were already too big when this landed are on a ratchet that only lets them shrink.
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase

from auctions import module_map

REPO_ROOT = Path(__file__).resolve().parent.parent


class ModuleMapIsCurrentTests(SimpleTestCase):
    def test_the_checked_in_map_matches_the_code_it_describes(self):
        modules = module_map.iter_modules()
        expected = module_map.render(modules)
        actual = module_map.OUTPUT_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            actual,
            expected,
            "docs/module_map.md no longer matches the modules. Run "
            "`python3 auctions/module_map.py --write` and commit the result.",
        )

    def test_the_generator_actually_looks_at_this_repo(self):
        """A generator pointed at nothing would agree with an empty map for ever."""
        modules = module_map.iter_modules()
        self.assertGreater(len(modules), 150, "expected to find the site's modules")
        found = {m.rel for m in modules}
        self.assertIn("auctions/models.py", found)
        self.assertIn("auctions/mcp/tools.py", found)

    def test_generated_files_are_left_out(self):
        found = {m.rel for m in module_map.iter_modules()}
        self.assertFalse(
            [rel for rel in found if "/migrations/" in rel],
            "migrations are the schema's history, not modules anybody reads as a map",
        )
        self.assertFalse([rel for rel in found if "/vendor/" in rel], "vendored code is not ours to describe")


class ModuleRulesTests(SimpleTestCase):
    """The three rules, checked against the real tree."""

    def test_every_module_obeys_the_size_and_docstring_rules(self):
        problems = module_map.rule_violations(module_map.iter_modules())
        if problems:
            report = "\n".join(f"  {problem}" for problem in problems)
            self.fail(f"{len(problems)} module(s) break the rules in auctions/module_map.py:\n{report}")

    def test_the_oversized_list_has_no_stale_entries(self):
        """An entry for a module that has been split, or has dropped under the limit, is noise."""
        by_path = {m.rel: m for m in module_map.iter_modules()}
        stale = []
        for rel in module_map.OVERSIZED:
            module = by_path.get(rel)
            if module is None:
                stale.append(f"{rel} no longer exists -- drop it from OVERSIZED")
            elif module.line_count <= module_map.NEW_MODULE_LINE_LIMIT:
                stale.append(
                    f"{rel} is down to {module.line_count} lines -- drop it from OVERSIZED so the "
                    f"{module_map.NEW_MODULE_LINE_LIMIT}-line limit applies to it like anything else"
                )
        self.assertEqual(stale, [], "\n".join(stale))


class RuleCheckerTests(SimpleTestCase):
    """The checker itself, so a refactor can't quietly turn it into a no-op."""

    @staticmethod
    def _module(rel, *, lines, docstring=True):
        body = '"""A summary line.\n\nMore.\n"""\n' if docstring else ""
        body += "x = 1\n" * lines
        return module_map.Module(Path(rel), source=body)

    def test_a_long_module_without_a_docstring_is_caught(self):
        problems = module_map.rule_violations([self._module("app/big.py", lines=400, docstring=False)])
        self.assertTrue(any("no module docstring" in p for p in problems))

    def test_a_long_module_with_a_docstring_is_fine_until_the_size_limit(self):
        self.assertEqual(module_map.rule_violations([self._module("app/ok.py", lines=400)]), [])

    def test_a_short_module_needs_no_docstring(self):
        self.assertEqual(module_map.rule_violations([self._module("app/tiny.py", lines=10, docstring=False)]), [])

    def test_a_new_module_over_the_limit_is_caught(self):
        problems = module_map.rule_violations([self._module("app/huge.py", lines=2000)])
        self.assertTrue(any("Split it" in p for p in problems))

    def test_a_listed_module_may_not_grow_past_its_allowance(self):
        allowance = module_map.OVERSIZED["auctions/forms.py"]
        problems = module_map.rule_violations([self._module("auctions/forms.py", lines=allowance + 50)])
        self.assertTrue(any("ratchet" in p for p in problems))

    def test_a_listed_module_under_its_allowance_passes(self):
        allowance = module_map.OVERSIZED["auctions/forms.py"]
        self.assertEqual(
            module_map.rule_violations([self._module("auctions/forms.py", lines=allowance - 100)]),
            [],
        )


class SummaryTests(SimpleTestCase):
    def test_the_summary_is_the_docstring_s_first_line(self):
        module = module_map.Module(Path("app/x.py"), source='"""What it does.\n\nWhy.\n"""\n')
        self.assertEqual(module.summary, "What it does.")

    def test_a_module_with_no_docstring_has_no_summary(self):
        self.assertEqual(module_map.Module(Path("app/x.py"), source="x = 1\n").summary, "")

    def test_private_names_are_not_listed(self):
        module = module_map.Module(Path("app/x.py"), source="def public():\n    pass\n\n\ndef _private():\n    pass\n")
        self.assertEqual(module.symbols, ["public"])


class ViewsPackageStaysAcyclicTests(SimpleTestCase):
    """`auctions/views/CLAUDE.md` promises an acyclic import graph. This is the promise, checked.

    The 34 modules were split out of one file, so a helper that two areas want is easy to import
    sideways -- and the day two modules want each other's, the site stops booting with a traceback
    that names neither of them as the cause. `base.py` is where a shared helper goes instead.
    """

    PACKAGE = Path(__file__).resolve().parent / "views"

    @classmethod
    def _graph(cls):
        """Which sibling modules each module in the package imports, by reading the source.

        Read rather than imported: `import auctions.views` resolves every one of these edges
        successfully, so a cycle is invisible from the inside once the package has loaded.
        """
        graph = {}
        for path in sorted(cls.PACKAGE.glob("*.py")):
            if path.name == "__init__.py":
                continue
            siblings = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if node.level == 1:
                    siblings.add(node.module.split(".")[0])
                elif node.module.startswith("auctions.views."):
                    siblings.add(node.module.split(".")[2])
            graph[path.stem] = siblings - {path.stem}
        return graph

    def test_the_package_finds_its_own_modules(self):
        """A graph builder that found nothing would call an empty package acyclic for ever."""
        graph = self._graph()
        self.assertGreater(len(graph), 20, "expected the views package's modules")
        self.assertIn("base", graph)

    def test_no_module_imports_a_sibling_that_imports_it_back(self):
        graph = self._graph()
        cycles = []
        visiting, done = set(), set()

        def walk(module, path):
            visiting.add(module)
            for sibling in sorted(graph.get(module, ())):
                if sibling in visiting:
                    cycles.append(" -> ".join([*path[path.index(sibling) :], sibling]))
                elif sibling not in done:
                    walk(sibling, [*path, sibling])
            visiting.discard(module)
            done.add(module)

        for module in sorted(graph):
            if module not in done:
                walk(module, [module])

        self.assertEqual(
            cycles,
            [],
            "auctions.views must stay acyclic -- move the shared helper into views/base.py rather "
            "than importing sideways:\n  " + "\n  ".join(cycles),
        )

    def test_only_base_is_imported_widely(self):
        """The rule is `base.py` for anything shared; a sibling edge is meant to be exceptional."""
        graph = self._graph()
        importers = {}
        for module, siblings in graph.items():
            for sibling in siblings:
                importers.setdefault(sibling, set()).add(module)
        popular = {name: sorted(mods) for name, mods in importers.items() if name != "base" and len(mods) > 3}
        self.assertEqual(
            popular,
            {},
            "a module other than base.py has become a shared dependency; move what they all want "
            f"into views/base.py: {popular}",
        )
