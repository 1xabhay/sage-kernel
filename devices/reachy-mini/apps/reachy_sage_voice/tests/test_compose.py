"""services/compose.yaml stays loopback-only and sealed.

Every host port is bound to 127.0.0.1, every service is read-only with all
capabilities dropped and no-new-privileges, and SearXNG has no host port at all.
The checks also run on planted bad services, so a check that never fires fails.
"""

import pathlib
import re

import yaml

COMPOSE = pathlib.Path(__file__).parents[1] / "services" / "compose.yaml"


def problems(compose: dict) -> list[str]:
    """Everything in ``compose`` that breaks the rules, one line each."""
    found = []
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            # Short syntax "127.0.0.1:8781:8000"; long syntax {host_ip, published, target}.
            host = port.get("host_ip") if isinstance(port, dict) else (
                str(port).rsplit(":", 2)[0] if str(port).count(":") == 2 else None)
            if host != "127.0.0.1":
                found.append(f"{name}: port {port} not bound to 127.0.0.1")
        if name == "searxng" and service.get("ports"):
            found.append("searxng: has a host port")
        if service.get("read_only") is not True:
            found.append(f"{name}: not read_only")
        if service.get("cap_drop") != ["ALL"]:
            found.append(f"{name}: cap_drop is not [ALL]")
        if "no-new-privileges:true" not in service.get("security_opt", []):
            found.append(f"{name}: no no-new-privileges")
    return found


SEALED = {"read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"]}


def test_the_real_compose_file_is_loopback_only_and_sealed():
    compose = yaml.safe_load(COMPOSE.read_text())
    assert set(compose["services"]) >= {"stt", "tts", "research", "searxng"}
    assert problems(compose) == []


def test_the_voice_s_senses_are_the_last_to_be_killed_or_starved_in_a_shared_vm():
    """A global OOM took the STT mid-session (live 2026-10-27): it was the biggest process in the VM."""
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    for name in ("stt", "tts"):
        assert services[name]["oom_score_adj"] < 0, name
        assert services[name]["cpu_shares"] > 1024, name  # Docker's default


def test_a_port_on_every_interface_is_caught():
    bad = {"services": {
        "a": {**SEALED, "ports": ["8781:8000"]},
        "b": {**SEALED, "ports": ["0.0.0.0:8782:8000"]},
        "c": {**SEALED, "ports": [{"target": 8000, "published": 8783}]},
        "d": {**SEALED, "ports": [{"host_ip": "127.0.0.1", "target": 8000, "published": 8784}]},
    }}
    assert [line.split(":")[0] for line in problems(bad)] == ["a", "b", "c"]


def test_searxng_with_any_host_port_is_caught():
    bad = {"services": {"searxng": {**SEALED, "ports": ["127.0.0.1:8080:8080"]}}}
    assert problems(bad) == ["searxng: has a host port"]


def test_an_unsealed_service_is_caught_on_each_count():
    bad = {"services": {"a": {"read_only": False, "cap_drop": ["NET_RAW"]}}}
    assert problems(bad) == ["a: not read_only", "a: cap_drop is not [ALL]",
                             "a: no no-new-privileges"]


MAKEFILE = COMPOSE.parent / "Makefile"


def binds(compose: dict) -> list[str]:
    """Every host folder or file a service mounts: config is baked in, state is a named volume."""
    found = []
    for name, service in compose["services"].items():
        for volume in service.get("volumes", []):
            source = volume.get("source", "") if isinstance(volume, dict) else str(volume).split(":")[0]
            if (isinstance(volume, dict) and volume.get("type") == "bind") or source.startswith((".", "/", "~", "$")):
                found.append(f"{name}: {source}")
    return found


def test_the_project_is_the_live_one_and_mounts_nothing_from_the_host():
    compose = yaml.safe_load(COMPOSE.read_text())
    assert compose["name"] == "reachy-sage-voice"
    assert binds(compose) == []
    assert binds({"services": {"a": {"volumes": ["./x:/x:ro", "/etc:/e", "named:/n"]},
                               "b": {"volumes": [{"type": "bind", "source": "y", "target": "/y"}]}}}) == [
        "a: ./x", "a: /etc", "b: y"]


def test_down_stops_the_services_and_keeps_their_volumes():
    make = MAKEFILE.read_text()
    recipe = make.split("\ndown:\n", 1)[1].split("\n\n", 1)[0]
    assert recipe.strip() == "docker compose down"
    assert "-v \"$(" not in make and "--volume" not in make  # enroll streams the recordings in


def test_searxng_settings_live_outside_the_images_volumes():
    """Upstream declares VOLUME /etc/searxng: a recreate keeps the old volume over a file baked there."""
    dockerfile = (COMPOSE.parent / "searxng" / "Dockerfile").read_text()
    path = re.search(r"(?m)^ENV SEARXNG_SETTINGS_PATH=(\S+)$", dockerfile).group(1)
    assert re.search(rf"(?m)^COPY settings.yml {re.escape(path)}$", dockerfile)
    assert not path.startswith(("/etc/searxng/", "/var/cache/searxng/"))
