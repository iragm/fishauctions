"""The map of this repository: which module does what, generated from the modules themselves.

There is no hand-written overview of this codebase and there deliberately never will be. An
overview is the kind of document that is true the day it is written and quietly wrong three months
later, and a wrong map is worse than no map -- it sends a reader to the wrong file with confidence.
So this file *derives* the map, out of two things that live next to the code and move when it
moves: each module's **docstring** and its **top-level names**. ``docs/module_map.md`` is the
output, it is checked in so it can be read without running anything, and
:mod:`auctions.test_module_map` fails the build if it stops matching what this script produces.

One check rides along with the generation, because the map is only as good as what it is reading:
**a module over** :data:`DOCSTRING_REQUIRED_OVER` **lines must have a module docstring.** That is
the whole anti-drift mechanism, stated as a rule. A docstring sits in the diff of the change that
invalidates it, which is the only reliable moment to fix a description; a separate document does
not, and that is why separate documents rot. The threshold is high enough that a small helper
module is not made to explain itself.

**There is no line limit here, and there should not be one.** Files in this repository get split
when splitting makes them easier to work in -- less to read to find the thing you came for -- and
that is a judgement about a particular file, not a number a script can hold anybody to. A line
count has no opinion about whether a module is doing one job or five.

Run it directly to check, or with ``--write`` to regenerate the map:

    python3 auctions/module_map.py            # check: map current, rules met (this is what CI runs)
    python3 auctions/module_map.py --write     # regenerate docs/module_map.md

It imports nothing from Django and touches no database, so it runs in a bare pre-commit
environment as happily as inside the container.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "docs" / "module_map.md"

HEADER = """<!-- GENERATED FILE -- do not edit by hand.
     Regenerate with: python3 auctions/module_map.py --write
     Every line below comes from a module's own docstring and top-level names, so this file cannot
     drift from the code; auctions/test_module_map.py fails the build if it has. Why it works this
     way is explained in auctions/module_map.py. -->

# Module map

One line per Python module: its first docstring line, and the top-level names it defines. This is
the "which file do I open" index. It is not documentation -- the docstring in the module is, and
this only quotes its opening sentence.
"""

# Directories whose contents are not this project's prose-worthy source: generated, vendored, or
# somebody else's. `migrations` is the big one -- 294 files that are the schema's history rather
# than modules anybody reads top to bottom.
SKIP_DIR_NAMES = frozenset(
    {
        "__pycache__",
        "migrations",
        "node_modules",
        "static",
        "templates",
        "vendor",
        ".venv",
        "venv",
    }
)

# Top-level directories that are not the application at all.
SKIP_TOP_LEVEL = frozenset({"swag", ".git", ".github", "logs", "mediafiles", "staticfiles"})

DOCSTRING_REQUIRED_OVER = 300

# A module's top-level names are listed only when there are few enough of them to be an answer.
# A truncated list ("+58 more") is not an index of anything -- grep does that job better -- and it
# was two thirds of this file's size when the map listed every module's names.
MAX_SYMBOLS_SHOWN = 10


class Module:
    """One Python file, as the map sees it."""

    def __init__(self, path: pathlib.Path, source: str | None = None) -> None:
        self.path = path
        try:
            self.rel = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            # A module built from source text in a test, which has no place in the tree.
            self.rel = path.as_posix()
        if source is None:
            source = path.read_text(encoding="utf-8", errors="replace")
        self.line_count = source.count("\n") + (1 if source and not source.endswith("\n") else 0)
        try:
            tree = ast.parse(source)
        except SyntaxError:
            self.docstring = None
            self.symbols: list[str] = []
            return
        self.docstring = ast.get_docstring(tree)
        self.symbols = [
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith("_")
        ]

    @property
    def summary(self) -> str:
        """The docstring's first line, which is the one sentence the map quotes."""
        if not self.docstring:
            return ""
        first = self.docstring.strip().split("\n", 1)[0].strip()
        return first

    @property
    def is_package(self) -> bool:
        return self.path.name == "__init__.py"


def iter_modules() -> list[Module]:
    """Every module the map covers, in path order."""
    found = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in SKIP_TOP_LEVEL:
            continue
        if any(part in SKIP_DIR_NAMES for part in rel.parts):
            continue
        found.append(Module(path))
    return found


def render(modules: list[Module]) -> str:
    """The whole of ``docs/module_map.md``."""
    lines = [HEADER]
    by_directory: dict[str, list[Module]] = {}
    for module in modules:
        directory = str(pathlib.PurePosixPath(module.rel).parent)
        by_directory.setdefault(directory, []).append(module)

    for directory in sorted(by_directory):
        entries = by_directory[directory]
        package = next((m for m in entries if m.is_package and m.summary), None)
        lines.append(f"\n## `{directory}/`\n")
        if package:
            lines.append(f"{package.summary}\n")
        for module in entries:
            if module.is_package and (package is module or not module.summary):
                # A package's own docstring is the directory's heading above; printing it again as
                # an `__init__.py` row says the same sentence twice.
                continue
            name = pathlib.PurePosixPath(module.rel).name
            lines.append(f"- **`{name}`** ({module.line_count} lines)")
            if module.summary:
                lines.append(f"  {module.summary}")
            if module.symbols and len(module.symbols) <= MAX_SYMBOLS_SHOWN:
                lines.append("  " + ", ".join(f"`{s}`" for s in module.symbols))
    return "\n".join(lines).rstrip() + "\n"


def rule_violations(modules: list[Module]) -> list[str]:
    """Every way the tree breaks the rule in this module's docstring."""
    problems = []
    for module in modules:
        if module.line_count > DOCSTRING_REQUIRED_OVER and not module.docstring:
            problems.append(
                f"{module.rel} is {module.line_count} lines and has no module docstring. "
                f"Anything over {DOCSTRING_REQUIRED_OVER} lines has to say what it is for -- one "
                f"paragraph at the top of the file, which is where it will be seen and kept true."
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--write", action="store_true", help="regenerate docs/module_map.md")
    args = parser.parse_args(argv)

    modules = iter_modules()
    rendered = render(modules)

    if args.write:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(rendered, encoding="utf-8")
        sys.stdout.write(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)} ({len(modules)} modules)\n")

    problems = rule_violations(modules)
    if not args.write:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        if current != rendered:
            problems.insert(
                0,
                "docs/module_map.md is out of date. Run `python3 auctions/module_map.py --write`.",
            )

    for problem in problems:
        sys.stderr.write(f"error: {problem}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
