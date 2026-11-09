"""Device protocol v1 over stdlib HTTP, against a scripted kernel on localhost.

Only the wire is real here; clocks and sleeps are injected so retries run instantly.
"""

import asyncio

import pytest
from conftest import TOKEN

from sage_device.kernel import KernelClient, KernelUnavailableError, Session, acted

#: Any body's manifest: the client only carries it.
MANIFEST = {"senses": ["speech"], "tools": []}

# --- the client ---------------------------------------------------------------


def client(kernel, **kw):
    return KernelClient(kernel.url, TOKEN, **kw)


def test_hello_declares_v1_and_the_manifest_with_the_token(kernel):
    kernel.script("POST", "/v1/hello", (200, {"protocol": 1, "sequence": 7}))
    assert client(kernel).hello(MANIFEST) == (200, {"protocol": 1, "sequence": 7})
    (req,) = kernel.requests
    assert req["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert req["body"] == {"protocols": [1], "manifest": MANIFEST}


def test_effects_long_polls_from_a_cursor_and_a_reply_offset(kernel):
    kernel.script("GET", "/v1/effects", (200, {"sequence": 9, "effects": []}))
    client(kernel).effects(since=9, wait=2, offset=12)
    assert kernel.requests[0]["query"] == {"since": ["9"], "wait": ["2"], "offset": ["12"]}


def test_rate_limited_events_wait_retry_after_then_go_through(kernel):
    kernel.script("POST", "/v1/events",
                  (429, {"code": "rate_limited", "retry_after": 0.25}), (200, {"sequence": 3}))
    slept = []
    status, _ = client(kernel, sleep=slept.append).event({"kind": "result", "sequence": 2,
                                                          "status": "done"})
    assert status == 200 and slept == [0.25]
    assert len(kernel.events("result")) == 2  # the same event, resent


def test_an_unreachable_kernel_is_not_an_oserror():
    dead = KernelClient("http://127.0.0.1:9", TOKEN, timeout_s=0.5)
    with pytest.raises(KernelUnavailableError) as caught:
        dead.hello(MANIFEST)
    assert not isinstance(caught.value, OSError)


def test_a_garbled_answer_is_an_outage_too(kernel):
    kernel.script("POST", "/v1/hello", (200, b"<html>proxy error</html>"))
    with pytest.raises(KernelUnavailableError):
        client(kernel).hello(MANIFEST)


def test_a_coded_error_comes_back_as_its_status_and_body(kernel):
    kernel.script("POST", "/v1/events", (409, {"code": "forbidden", "message": "not yours"}))
    assert client(kernel).event({"kind": "acknowledged", "turn_id": "x"})[0] == 409


# --- the session: one conversation's rules ------------------------------------


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def session(kernel, clock=None):
    clock = clock or Clock()

    async def sleep(s):
        clock.t += s

    return Session(client(kernel), MANIFEST, retry_s=5.0, clock=clock, sleep=sleep)


def hello_ok(kernel, sequence=40):
    kernel.script("POST", "/v1/hello", (200, {"protocol": 1, "sequence": sequence}))


def test_submitting_says_hello_first_then_sends_the_turn_with_timing(kernel):
    hello_ok(kernel)
    kernel.script("POST", "/v1/events", (200, {"sequence": 41}))
    s = session(kernel)
    assert asyncio.run(s.start("reachy-1", "hi", {"stt_ms": 180})) == "admitted"
    assert [r["path"] for r in kernel.requests] == ["/v1/hello", "/v1/events"]
    assert kernel.events() == [{"kind": "submitted", "turn_id": "reachy-1", "text": "hi",
                                "timing": {"stt_ms": 180}}]
    assert s.cursor == 40  # a fresh body starts at the head, never replaying old replies


#: What a v1.1 kernel serves (sage-kernel docs/PROTOCOL.md §1).
V11 = ["acted", "body", "done_text", "health", "input", "notes", "presence"]


def hello_v11(kernel, sequence=40):
    kernel.script("POST", "/v1/hello",
                  (200, {"protocol": 1, "sequence": sequence, "features": V11}))


def test_hello_is_once_per_connect_not_once_per_turn(kernel):
    hello_ok(kernel)
    kernel.script("POST", "/v1/events", (200, {"sequence": 41}))
    kernel.script("GET", "/v1/effects", (200, {"sequence": 41, "effects": []}))
    s = session(kernel)

    async def go():
        await s.start("reachy-1", "hi", {})
        await s.poll(offset=0, wait=0)
        await s.start("reachy-2", "again", {})
    asyncio.run(go())
    assert [r["path"] for r in kernel.requests] == [
        "/v1/hello", "/v1/events", "/v1/effects", "/v1/events"]


def test_an_outage_says_hello_again_the_kernel_may_have_forgotten_the_manifest(kernel):
    hello_ok(kernel)
    kernel.script("POST", "/v1/events", (200, {"sequence": 41}),
                  (200, b"<html>proxy error</html>"), (200, {"sequence": 43}))
    s = session(kernel)

    async def go():
        await s.start("reachy-1", "hi", {})
        with pytest.raises(KernelUnavailableError):
            await s.start("reachy-2", "hi", {})
        await s.start("reachy-3", "hi", {})
    asyncio.run(go())
    assert [r["path"] for r in kernel.requests] == [
        "/v1/hello", "/v1/events", "/v1/events", "/v1/hello", "/v1/events"]


def test_a_reset_journal_says_hello_again(kernel):
    hello_ok(kernel)
    kernel.script("POST", "/v1/events", (200, {"sequence": 41}))
    kernel.script("GET", "/v1/effects", (409, {"code": "reset", "sequence": 12}))
    s = session(kernel)

    async def go():
        await s.start("reachy-1", "hi", {})
        await s.poll(offset=0, wait=0)
        await s.start("reachy-2", "hi", {})
    asyncio.run(go())
    assert [r["path"] for r in kernel.requests] == [
        "/v1/hello", "/v1/events", "/v1/effects", "/v1/hello", "/v1/events"]
    assert s.cursor == 12  # the reset's, not the new hello's head


HELLO_FIRST = (409, {"code": "hello_first", "message": "say hello first"})


def test_hello_first_says_hello_again_and_retries_the_request_once(kernel):
    hello_ok(kernel)
    kernel.script("POST", "/v1/events", HELLO_FIRST, (200, {"sequence": 41}))
    kernel.script("GET", "/v1/effects", HELLO_FIRST, (200, {"sequence": 42, "effects": []}))
    s = session(kernel)

    async def go():
        admitted = await s.start("reachy-1", "hi", {})
        return admitted, await s.poll(offset=0, wait=0)
    admitted, polled = asyncio.run(go())
    assert admitted == "admitted" and polled["sequence"] == 42
    assert [r["path"] for r in kernel.requests] == [
        "/v1/hello", "/v1/events", "/v1/hello", "/v1/events",
        "/v1/effects", "/v1/hello", "/v1/effects"]
    hellos = [r["body"] for r in kernel.requests if r["path"] == "/v1/hello"]
    assert all(h["manifest"] == MANIFEST for h in hellos)


def test_hello_first_twice_in_a_row_gives_up_instead_of_looping(kernel):
    hello_ok(kernel)
    kernel.script("POST", "/v1/events", HELLO_FIRST)
    s = session(kernel)
    assert asyncio.run(s.start("reachy-1", "hi", {})) == "refused"
    assert [r["path"] for r in kernel.requests] == [
        "/v1/hello", "/v1/events", "/v1/hello", "/v1/events"]


def test_features_come_from_hello_and_a_kernel_without_them_is_plain_v1(kernel):
    hello_ok(kernel)
    s = session(kernel)
    assert s.features is None  # nothing said yet
    asyncio.run(s.hello())
    assert s.features == frozenset()
    hello_v11(kernel)
    asyncio.run(s.hello())
    assert s.features == frozenset(V11)


def test_a_v11_kernel_hears_how_the_line_came_in(kernel):
    hello_v11(kernel)
    kernel.script("POST", "/v1/events", (200, {"sequence": 41}))
    asyncio.run(session(kernel).start("reachy-1", "hi", {}, {"medium": "speech"}))
    assert kernel.events() == [{"kind": "submitted", "turn_id": "reachy-1", "text": "hi",
                                "input": {"medium": "speech"}}]


def test_a_plain_v1_kernel_never_sees_input(kernel):
    hello_ok(kernel)
    kernel.script("POST", "/v1/events", (200, {"sequence": 41}))
    asyncio.run(session(kernel).start("reachy-1", "hi", {}, {"medium": "speech"}))
    assert kernel.events() == [{"kind": "submitted", "turn_id": "reachy-1", "text": "hi"}]


def test_acts_go_to_a_kernel_that_serves_them(kernel):
    hello_v11(kernel)
    kernel.script("POST", "/v1/events", (200, {"sequence": 41}))
    s = session(kernel)
    act = acted("act-1", "aside", "done", said="One moment.")

    async def go():
        await s.act(act)  # before any hello: what the kernel serves is unknown, so nothing
        await s.hello()
        await s.act(act)
    asyncio.run(go())
    assert kernel.events() == [act]


def test_a_plain_v1_kernel_never_sees_acts(kernel):
    hello_ok(kernel)
    kernel.script("POST", "/v1/events", (200, {"sequence": 41}))
    s = session(kernel)

    async def go():
        await s.hello()
        await s.act(acted("act-1", "aside", "done", said="One moment."))
    asyncio.run(go())
    assert kernel.events() == []


def test_acted_is_the_v11_event_with_unset_fields_left_out():
    assert acted("act-1", "aside", "done", said="Looking for you.") == {
        "kind": "acted", "act_id": "act-1", "origin": "reflex", "what": "aside",
        "status": "done", "said": "Looking for you."}
    plan = {"steps": [{"skill": "search_face", "until": "face_found"}]}
    event = acted("act-2", "body_plan", "failed", input=plan, reason="x" * 100,
                  said="y" * 5000)
    assert event["input"] == plan
    assert len(event["reason"]) == 64 and len(event["said"]) == 4000  # the kernel's limits


def test_busy_retries_the_same_turn_id(kernel):
    hello_ok(kernel)
    kernel.script("POST", "/v1/events", (409, {"code": "busy"}), (409, {"code": "busy"}),
                  (200, {"sequence": 41}))
    assert asyncio.run(session(kernel).start("reachy-1", "hi", {})) == "admitted"
    assert [e["turn_id"] for e in kernel.events("submitted")] == ["reachy-1"] * 3


def test_busy_for_about_5_s_gives_up(kernel):
    hello_ok(kernel)
    kernel.script("POST", "/v1/events", (409, {"code": "busy"}))
    clock = Clock()
    assert asyncio.run(session(kernel, clock).start("reachy-1", "hi", {})) == "busy"
    assert 5.0 <= clock.t < 6.0


def test_unknown_pending_acknowledges_this_bodys_own_turn_then_retries(kernel):
    hello_ok(kernel)
    kernel.script("POST", "/v1/events", (409, {"code": "unknown_pending"}),
                  (200, {"sequence": 42}), (200, {"sequence": 43}))
    kernel.script("GET", "/v1/effects", (200, {"sequence": 40, "effects": [],
                                              "turn": {"turn_id": "reachy-0",
                                                       "run_status": "unknown"}}))
    assert asyncio.run(session(kernel).start("reachy-1", "hi", {})) == "admitted"
    assert [(e["kind"], e["turn_id"]) for e in kernel.events()] == [
        ("submitted", "reachy-1"), ("acknowledged", "reachy-0"), ("submitted", "reachy-1")]


def test_another_bodys_pending_turn_is_not_ours_to_acknowledge(kernel):
    hello_ok(kernel)
    kernel.script("POST", "/v1/events", (409, {"code": "unknown_pending"}))
    kernel.script("GET", "/v1/effects", (200, {"sequence": 40, "effects": []}))
    assert asyncio.run(session(kernel).start("reachy-1", "hi", {})) == "refused"
    assert kernel.events("acknowledged") == []


def test_poll_advances_the_cursor_and_passes_the_offset(kernel):
    hello_ok(kernel)
    kernel.script("POST", "/v1/events", (200, {"sequence": 41}))
    kernel.script("GET", "/v1/effects", (200, {"sequence": 44, "effects": [
        {"kind": "reply_delta", "turn_id": "reachy-1", "offset": 0, "text": "Hel"}]}))
    s = session(kernel)

    async def go():
        await s.start("reachy-1", "hi", {})
        return await s.poll(offset=0, wait=2)
    body = asyncio.run(go())
    assert body["effects"][0]["text"] == "Hel" and s.cursor == 44
    assert kernel.requests[-1]["query"]["since"] == ["40"]


def test_a_reset_restarts_from_the_kernels_sequence(kernel):
    hello_ok(kernel, sequence=900)
    kernel.script("POST", "/v1/events", (200, {"sequence": 901}))
    kernel.script("GET", "/v1/effects", (409, {"code": "reset", "sequence": 12}))
    s = session(kernel)

    async def go():
        await s.start("reachy-1", "hi", {})
        return await s.poll(offset=0, wait=0)
    assert asyncio.run(go())["effects"] == [] and s.cursor == 12


def test_results_and_stop_go_out_as_events(kernel):
    kernel.script("POST", "/v1/events", (200, {"sequence": 50}))
    s = session(kernel)

    async def go():
        await s.result(42, "interrupted", text="Hello, I was", timing={"first_audio_ms": 700})
        await s.result(41, "done", duration_ms=2300)
        await s.result(43, "failed", reason="stale")
        await s.result(None, "interrupted", text="Once upon", turn_id="reachy-2")
        await s.stop("reachy-1")
    asyncio.run(go())
    assert kernel.events() == [
        {"kind": "result", "sequence": 42, "status": "interrupted", "text": "Hello, I was",
         "timing": {"first_audio_ms": 700}},
        {"kind": "result", "sequence": 41, "status": "done", "duration_ms": 2300},
        {"kind": "result", "sequence": 43, "status": "failed", "reason": "stale"},
        # A reply still streaming has no sequence yet: it is named by its turn
        # (assumed shape of the pending v1 amendment for streaming devices).
        {"kind": "result", "turn_id": "reachy-2", "status": "interrupted", "text": "Once upon"},
        {"kind": "stop", "turn_id": "reachy-1"}]


def test_presence_goes_out_as_an_event(kernel):
    kernel.script("POST", "/v1/events", (200, {"sequence": 7}))
    asyncio.run(session(kernel).presence("seen", 1.0))
    assert kernel.events() == [{"kind": "presence", "state": "seen", "confidence": 1.0}]


