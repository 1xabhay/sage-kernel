"""Print the folders outside the app that it builds from, one "<as written> <from the top>" per line.

Those are [tool.uv.sources] path entries and [tool.setuptools.packages.find] `where` folders
other than the app's own (the wheel bundles sage_device from one).
`python3 -I deploy/sources.py <the app's prefix from the top> < pyproject.toml`. Stdlib only.
"""

import os
import sys
import tomllib

prefix = sys.argv[1] if len(sys.argv) > 1 else ""
tool = tomllib.loads(sys.stdin.read()).get("tool", {})
paths = [s["path"] for s in tool.get("uv", {}).get("sources", {}).values() if "path" in s]
paths += tool.get("setuptools", {}).get("packages", {}).get("find", {}).get("where", [])
for path in dict.fromkeys(paths):
    if os.path.normpath(path) != ".":
        print(path, os.path.normpath(os.path.join(prefix, path)))
