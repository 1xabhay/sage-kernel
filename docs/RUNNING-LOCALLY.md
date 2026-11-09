# Running locally

Two kinds of run. **Throwaway** runs make their own state and leave nothing behind; use them
anywhere, worktrees included. **Live** runs are the agent itself: its journal, its robot. They are
the serial lane: one at a time, from the main checkout, owner aware.

## Throwaway

```sh
make check                                                  # kernels in sage-kernel-test, no volumes
make -C devices/reachy-mini/apps/reachy_sage_voice integration   # a real kernel on a free loopback port, fake model
make -C devices/reachy-mini/apps/reachy_sim sim             # Pollen's MuJoCo sim on 127.0.0.1:8742, never the robot
make -C devices/reachy-mini/apps/reachy_sage_voice/services native-stt   # STT on 127.0.0.1:8791
make -C devices/reachy-mini/apps/reachy_sage_voice/services native-tts   # TTS on 127.0.0.1:8792
```

The sim takes `PORT=` and `SCENE=` (default `minimal`). A voice-against-sim demo on a
throwaway kernel is coming (plan B3); until it lands, do not point anything at 8776 to try
the sim: that is the live kernel.

## Live (serial lane)

| What | Start | Stop | Where |
| --- | --- | --- | --- |
| Kernel | `make up` | `make down` | `127.0.0.1:8776`, journal in the `kernel-data` volume |
| Voice services (STT, TTS) | `make -C devices/reachy-mini/apps/reachy_sage_voice/services up` | (coming: plan B3, `down`) | loopback |
| Voice app | `make -C devices/reachy-mini/apps/reachy_sage_voice run` | Ctrl-C | on the Mac, talks to `<robot-host>` |
| sage-os (experiment) | `make -C experiments/sage-os up` | `make -C experiments/sage-os down` | `127.0.0.1:8786` |

- `make up` reads `.env` (copy `.env.example`, `chmod 600`): `SAGE_DEVICES` is
  `name=token,...`, a token of at least 32 random characters per device. Ollama runs on the host.
- `make down` stops the kernel and keeps the journal. Never add `-v`.
- The voice reads the `.env` in its app folder (`SAGE_VOICE_*`, `REACHY_MINI_HOST=<robot-host>`).
  Today it can also pick up a `.env` above it, such as the kernel's; the voice lane fixes that (plan A, voice settings).
  The robot must be on, on the same network, with its motor backend started.
- `make up` and `make down` will refuse in a linked worktree (coming: repo/guards). Until then,
  run them only from the main checkout.
- Status of the kernel and the robot: no target yet (coming: plan C2, `status`).

Deploying a new version of any of these: [DEPLOYING.md](DEPLOYING.md).
