"""Everything main.py imports locally must actually reach the device.

`decky plugin build` zips a fixed allowlist — main.py, plugin.json,
package.json, dist/, py_modules/, LICENSE, README.md — and nothing else. A
top-level package or module sitting next to main.py is not copied, so it
imports fine in the repo, imports fine in this test suite, and then fails at
plugin startup on the Deck with an ImportError.

.vscode/build.sh works around that by copying the local packages into
py_modules/, which *is* packaged. That list is maintained by hand, and this
test is what stops it drifting from what main.py actually imports — the one
failure mode the rest of the suite structurally cannot see, because pytest
runs from the repo root where everything is importable.

This caught a real one: settings_schema.py was added as a top-level module and
would have shipped a plugin that could not start.
"""

import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_SH = os.path.join(ROOT, ".vscode", "build.sh")

# What the decky CLI copies without help. Everything else that main.py needs
# has to be vendored into py_modules/ by build.sh.
CLI_ALLOWLIST = {"main.py", "plugin.json", "package.json", "dist", "py_modules",
                 "LICENSE", "README.md"}


def _local_top_level_imports(path):
    """Top-level names imported by `path` that resolve to something in-repo."""
    tree = ast.parse(open(path).read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])

    local = set()
    for name in names:
        if os.path.isdir(os.path.join(ROOT, name)) and os.path.exists(
            os.path.join(ROOT, name, "__init__.py")
        ):
            local.add(name)
        elif os.path.exists(os.path.join(ROOT, f"{name}.py")):
            local.add(name)
    return local


def _vendored_by_build_sh():
    """Names build.sh copies into py_modules/."""
    script = open(BUILD_SH).read()
    return set(re.findall(r"^\s*cp -r (\S+) py_modules/", script, re.MULTILINE))


def test_every_local_import_is_vendored():
    missing = _local_top_level_imports(os.path.join(ROOT, "main.py")) - _vendored_by_build_sh()
    assert not missing, (
        f"main.py imports {sorted(missing)}, which .vscode/build.sh does not copy "
        f"into py_modules/. The decky CLI packages only {sorted(CLI_ALLOWLIST)}, so "
        f"this would import fine here and fail on the Deck. Add a "
        f"`cp -r <name> py_modules/<name>` line to build.sh."
    )


def test_vendored_packages_all_exist():
    """A stale entry means build.sh fails mid-run, after wiping py_modules."""
    for name in _vendored_by_build_sh():
        assert os.path.exists(os.path.join(ROOT, name)), (
            f"build.sh copies {name!r}, which does not exist"
        )


@pytest.mark.parametrize("name", ["sources", "nfc", "cards", "decky_links"])
def test_known_local_packages_are_vendored(name):
    """Pinned explicitly so removing a cp line is a test failure, not a silent
    change that only shows up on a device."""
    assert name in _vendored_by_build_sh()


def test_local_packages_are_importable_as_packages():
    """Vendoring copies directories, so a local package without __init__.py
    would arrive on the device as a namespace package sharing a name with
    whatever else is on sys.path — py_modules holds every pip dependency."""
    for name in _vendored_by_build_sh():
        path = os.path.join(ROOT, name)
        if os.path.isdir(path) and name != "assets":
            assert os.path.exists(os.path.join(path, "__init__.py")), (
                f"{name}/ is vendored but has no __init__.py"
            )
