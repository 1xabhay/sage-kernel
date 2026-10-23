# Disposable local gateway probe

Purpose: verify the published connector's authentication against a pinned
gateway without sending model requests. This is a developer probe, not the
installer or Linux release. Read [EVIDENCE.md](EVIDENCE.md) for observed results.

## Existing disposable paths

| Path | Contains |
| --- | --- |
| `/private/tmp/sage-m2-node` | Node 24.16.0 installation |
| `/private/tmp/sage-m2-openclaw` | OpenClaw 2026.10.2 and npm package lock |
| `/private/tmp/sage-m2-python` | Python 3.12 virtual environment with Pipecat 1.12.0 |
| `/private/tmp/sage-m2-state` | Private config, generated test credential, gateway state/workspace |
| `/private/tmp/sage-npm-cache`, `/private/tmp/sage-uv-cache` | Download caches |

These paths are temporary and may disappear. The gateway is not installed as a
system service. Never share the state directory or copy its credential into
firmware. No provider credential is required for the handshake tests.

## Re-create if temporary files are gone

Use Python 3.12 and `uv`. Install only in the named disposable directories:

```sh
npm --cache /private/tmp/sage-npm-cache install --prefix /private/tmp/sage-m2-node --no-audit --no-fund node@24.16.0
env PATH=/private/tmp/sage-m2-node/node_modules/.bin:$PATH npm --cache /private/tmp/sage-npm-cache install --prefix /private/tmp/sage-m2-openclaw --no-audit --no-fund openclaw@2026.10.2
uv --cache-dir /private/tmp/sage-uv-cache venv --python 3.12 /private/tmp/sage-m2-python
uv --cache-dir /private/tmp/sage-uv-cache pip install --python /private/tmp/sage-m2-python/bin/python pipecat-ai==1.12.0
```

These pin the direct test dependencies. They do not freeze every transitive
dependency and are not a reproducible release lock. Preserve and review the
resolved locks before product packaging at M5.

Create a new disposable config only when that state directory is absent.
The following refuses to overwrite an existing directory:

```sh
python3 - <<'PY'
import json
import secrets
from pathlib import Path

state_dir = Path('/private/tmp/sage-m2-state')
state_dir.mkdir(mode=0o700)
config = {
    'gateway': {
        'mode': 'local', 'bind': 'loopback', 'port': 18791,
        'auth': {'mode': 'token', 'token': secrets.token_urlsafe(32)},
    },
    'agents': {'defaults': {
        'workspace': str(state_dir / 'workspace'),
        'compaction': {'memoryFlush': {'enabled': False}},
    }},
    'tools': {'deny': ['*']},
    'cron': {'enabled': False},
    'plugins': {'entries': {'memory-core': {'config': {
        'dreaming': {'enabled': False},
    }}}},
}
config_path = state_dir / 'openclaw.json'
config_path.touch(mode=0o600, exist_ok=False)
config_path.write_text(json.dumps(config, indent=2) + '\n')
PY
```

## Start, test, stop

Start in a terminal using the existing private config. Keep this terminal open:

```sh
OPENCLAW_STATE_DIR=/private/tmp/sage-m2-state \
OPENCLAW_CONFIG_PATH=/private/tmp/sage-m2-state/openclaw.json \
  /private/tmp/sage-m2-node/node_modules/node/bin/node \
  /private/tmp/sage-m2-openclaw/node_modules/openclaw/openclaw.mjs \
  gateway run --port 18791 --bind loopback
```

Wait for the ready message, then from this repo in a second terminal run:

```sh
SAGE_TEST_GATEWAY_CONFIG=/private/tmp/sage-m2-state/openclaw.json \
  /private/tmp/sage-m2-python/bin/python -B -m unittest discover -s tests -p test_gateway.py -v
```

Stop the gateway with Ctrl-C in the first terminal. If the port is occupied,
use a separate documented port/config; do not use `--force` to kill an unknown
listener. Gateway startup may perform upstream plugin discovery, catalog/update
checks, and local network discovery even with scheduled jobs disabled. This
probe does not establish a fully offline runtime.

The test reads the generated credential internally and checks loopback binding.
It exercises valid and deliberately invalid authentication, performs cleanup,
and sends no model turn. Default test discovery skips it unless
`SAGE_TEST_GATEWAY_CONFIG` is explicitly set.

To extend M2, first choose/configure a test reasoning provider outside the repo.
Then document the bounded notes policy and run actual turn/tool tests. Do not
remove the blanket tool denial or enable scheduling until the next contract
names the allowed capabilities and its acceptance tests.
