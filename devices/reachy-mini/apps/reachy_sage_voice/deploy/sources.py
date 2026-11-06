"""Print pyproject's [tool.uv.sources] path entries, one "<as written> <from the top>" per line.

`python3 -I deploy/sources.py <the app's prefix from the top> < pyproject.toml`. Stdlib only.
"""

import os
import sys
import tomllib

prefix = sys.argv[1] if len(sys.argv) > 1 else ""
uv = tomllib.loads(sys.stdin.read()).get("tool", {}).get("uv", {})
for source in uv.get("sources", {}).values():
    if "path" in source:
        print(source["path"], os.path.normpath(os.path.join(prefix, source["path"])))
