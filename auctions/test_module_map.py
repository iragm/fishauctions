"""Guards the module map against drift and verifies module docstring rules are enforced."""

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
    def test_every_long_module_says_what_it_is_for(self):
        problems = module_map.rule_violations(module_map.iter_modules())
        if problems:
            report = "\n".join(f"  {problem}" for problem in problems)
            self.fail(f"{len(problems)} module(s) break the rule in auctions/module_map.py:\n{report}")


class RuleCheckerTests(SimpleTestCase):
    @staticmethod
    def _module(rel, *, lines, docstring=True):
        body = '"""A summary line.\n\nMore.\n"""\n' if docstring else ""
        body += "x = 1\n" * lines
        return module_map.Module(Path(rel), source=body)

    def test_a_long_module_without_a_docstring_is_caught(self):
        problems = module_map.rule_violations([self._module("app/big.py", lines=400, docstring=False)])
        self.assertTrue(any("no module docstring" in p for p in problems))

    def test_a_long_module_with_a_docstring_is_fine(self):
        self.assertEqual(module_map.rule_violations([self._module("app/ok.py", lines=400)]), [])

    def test_a_short_module_needs_no_docstring(self):
        self.assertEqual(module_map.rule_violations([self._module("app/tiny.py", lines=10, docstring=False)]), [])

    def test_a_very_long_module_is_not_a_problem_on_its_own(self):
        self.assertEqual(module_map.rule_violations([self._module("app/huge.py", lines=20000)]), [])


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
