# Known vulnerabilities in this project's uv.lock (uv audit, from the advisory databases): needs the network.
# ponytail: starlette's advisories are ignored because reachy-mini 1.10.0 requires starlette<1.0.0 and
# every fix is in 1.x. Drop these the day the SDK lifts that cap (`uv lock -P starlette`, then re-audit).
STARLETTE_UNDER_SDK_CAP := GHSA-82w8-qh3p-5jfq GHSA-86qp-5c8j-p5mr GHSA-jp82-jpqv-5vv3 GHSA-wqp7-x3pw-xc5r GHSA-x746-7m8f-x49c
AUDIT := uv audit --locked --preview-features audit $(STARLETTE_UNDER_SDK_CAP:%=--ignore %)
