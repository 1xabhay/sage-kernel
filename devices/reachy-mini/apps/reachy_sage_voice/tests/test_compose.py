"""services/compose.yaml stays loopback-only and sealed.

Every host port is bound to 127.0.0.1, every service is read-only with all
capabilities dropped and no-new-privileges, and SearXNG has no host port at all.
The checks also run on planted bad services, so a check that never fires fails.
"""

import os
import pathlib
import re
import subprocess

import pytest
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


def test_enroll_tars_in_a_subshell_so_compose_runs_beside_its_file():
    """A bare cd into the recordings runs docker compose there, with no compose.yaml."""
    recipe = MAKEFILE.read_text().split("\nenroll:\n", 1)[1].split("\n\n", 1)[0]
    assert '(cd "$(RECORDINGS)" && COPYFILE_DISABLE=1 tar -c *.wav) | $(VOICEPRINT)' in recipe


def test_searxng_settings_live_outside_the_images_volumes():
    """Upstream declares VOLUME /etc/searxng: a recreate keeps the old volume over a file baked there."""
    dockerfile = (COMPOSE.parent / "searxng" / "Dockerfile").read_text()
    path = re.search(r"(?m)^ENV SEARXNG_SETTINGS_PATH=(\S+)$", dockerfile).group(1)
    assert re.search(rf"(?m)^COPY settings.yml {re.escape(path)}$", dockerfile)
    assert not path.startswith(("/etc/searxng/", "/var/cache/searxng/"))


# make -C services deploy / up-tag: one image per commit and service, rolled back if unhealthy.

SHA_BUILT = ("stt", "research", "searxng")  # tts runs natively (pocket-on) and keeps its tag


def recipe(target: str) -> str:
    return MAKEFILE.read_text().split(f"\n{target}:", 1)[1].split("\n\n", 1)[0]


def test_the_docker_services_run_the_tag_a_deploy_names_and_tts_keeps_its_own():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    for name in SHA_BUILT:
        assert services[name]["image"] == f"reachy-sage-{name}:${{SAGE_SERVICES_TAG:-dev}}", name
    assert services["tts"]["image"] == "reachy-sage-tts:0.3.0"


def test_the_voiceprint_volume_keeps_its_name_whatever_the_project():
    compose = yaml.safe_load(COMPOSE.read_text())
    assert compose["volumes"]["profiles"] == {"name": "reachy-sage-voice-profiles"}
    assert "profiles:/profiles:ro" in compose["services"]["stt"]["volumes"]


def test_deploy_builds_each_image_from_the_commit_and_rolls_back_through_up_tag():
    deploy, up_tag = recipe("deploy"), recipe("up-tag")
    # From the top: run in a subfolder, git archive of a tree keeps only that subfolder's paths (live: empty).
    assert 'git -C "$$top" archive "$$sha:$$here$$s" | docker build' in deploy
    assert "docker compose build" not in deploy and "--build" not in deploy + up_tag
    # Live 2026-11-09: a second STT beside the live one ran the VM out of memory, and the owner's voiceprint
    # dropped the test clip. The live STT stops for the test, which never reads the voiceprint, and a failure
    # brings the running tag back.
    assert deploy.index("docker compose stop stt") < deploy.index("python -m unittest")
    assert "-e SAGE_VOICEPRINT=/nonexistent" in deploy
    assert deploy.count("up-tag SAGE_SERVICES_TAG=$$prev") == 2
    assert "up-tag SAGE_SERVICES_TAG=$$prev" in deploy
    assert "docker compose up -d --no-build --wait $(SERVICES)" in up_tag
    assert "$(HEALTH)" in up_tag
    assert "$(HOME)/Library/Logs/reachy-sage/DEPLOYS.log" in MAKEFILE.read_text()


FAKE_DOCKER = """#!/bin/sh
echo "$SAGE_SERVICES_TAG $*" >> "$FAKE_LOG"
case "$1 $*" in
  "inspect "*) echo reachy-sage-stt:prev1 ;;
  "build "*) tar -t | grep -qx Dockerfile ;;  # a context with no Dockerfile fails, as docker's does
  "compose "*" up "*) [ "$SAGE_SERVICES_TAG" = prev1 ] || [ -n "$UP_OK" ] ;;
esac
"""


def in_checkout() -> bool:
    done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=COMPOSE.parent, capture_output=True)
    return done.returncode == 0


@pytest.mark.skipif(not in_checkout(), reason="deploy archives a commit: needs the git checkout")
@pytest.mark.parametrize("healthy", [True, False])
def test_deploy_brings_up_the_new_tag_or_rolls_back_to_the_running_one(tmp_path, healthy):
    bin = tmp_path / "bin"
    bin.mkdir()
    (bin / "docker").write_text(FAKE_DOCKER)
    (bin / "curl").write_text('#!/bin/sh\necho \'{"ready":true,"enrolled":false}\'\n')
    for tool in bin.iterdir():
        tool.chmod(0o755)
    # The app's Makefile exports the real DEPLOY_LOG (live 2026-11-09: make test wrote to it).
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MAKE", "MFLAGS", "SAGE_", "DEPLOY_")) and k != "SHA"}
    log = tmp_path / "Library/Logs/reachy-sage/DEPLOYS.log"
    env.update(PATH=f"{bin}:{env['PATH']}", HOME=str(tmp_path),
               FAKE_LOG=str(tmp_path / "docker.log"),
               **({"UP_OK": "1"} if healthy else {}))
    done = subprocess.run(["make", "-s", "deploy", "SHA=HEAD", f"DEPLOY_LOG={log}"],
                          cwd=COMPOSE.parent, env=env,
                          capture_output=True, text=True, timeout=120)
    sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=COMPOSE.parent,
                         capture_output=True, text=True, check=True).stdout.strip()
    calls = (tmp_path / "docker.log").read_text().splitlines()
    assert [c.split()[-1] for c in calls if " build " in c] == ["-"] * len(SHA_BUILT)
    ups = [c.split()[0] for c in calls if " up " in c]
    deploys = log.read_text()
    if healthy:
        assert done.returncode == 0, done.stderr
        assert ups == [sha]
        assert f"deploy {sha} services" in deploys
    else:
        assert done.returncode != 0
        assert ups == [sha, "prev1"]
        assert f"rollback {sha} services to prev1" in deploys
