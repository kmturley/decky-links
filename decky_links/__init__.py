"""Plugin internals, extracted from main.py.

A package rather than top-level modules because of how the plugin is
packaged: `decky plugin build` zips a fixed allowlist — main.py, plugin.json,
package.json, dist/, py_modules/, LICENSE, README.md — and nothing else. A
top-level module sitting next to main.py is simply not copied, so it imports
fine in the repo and in tests and then fails on the device.

.vscode/build.sh vendors this directory into py_modules/ alongside sources/,
nfc/ and cards/ for exactly that reason, and tests/test_packaging.py fails if
a local import is ever added without being vendored.
"""
