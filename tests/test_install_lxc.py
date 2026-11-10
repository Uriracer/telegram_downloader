"""deploy/install-lxc.sh against the real media-stack compose file, with stub docker/git/mountpoint."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "install-lxc.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(sys.platform == "win32" and not os.environ.get("TGDL_TEST_BASH"),
                                reason="bash script (set TGDL_TEST_BASH=1 to run it with Git Bash)")

STUBS = {
    # docker: "compose config -q" validates the YAML with Python; the rest only logs.
    "docker": f"""#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
if [ "$1 $2" = "compose version" ]; then exit 0; fi
if [ "$1" = compose ] && [ "$4" = config ]; then
  exec "{sys.executable}" -c "import sys, yaml; d = yaml.safe_load(open(sys.argv[1], encoding='utf-8')); \
assert 'telegram-downloader' in d['services']" "$3"
fi
exit 0
""",
    "mountpoint": '#!/usr/bin/env bash\n[ "${STUB_MOUNTED:-1}" = 1 ]\n',
    "git": """#!/usr/bin/env bash
echo "git $*" >> "$STUB_LOG"
if [ "$1" = clone ]; then dest="${@: -1}"; mkdir -p "$dest/.git" "$dest/tgdl";
  echo '__version__ = "9.9.9"' > "$dest/tgdl/__init__.py"; fi
if [ "$3" = rev-parse ]; then echo abc1234; fi
exit 0
""",
    "id": '#!/usr/bin/env bash\necho 0\n',
    "hostname": '#!/usr/bin/env bash\necho 192.168.4.20\n',
}


def run(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        (bin_dir / name).write_text(body, encoding="utf-8", newline="\n")
        (bin_dir / name).chmod(0o755)
    nas = tmp_path / "Plex"
    nas.mkdir(exist_ok=True)
    full_env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "COMPOSE_FILE": str(tmp_path / "media" / "docker-compose.yml"), "NAS_DIR": str(nas),
                "STUB_LOG": str(tmp_path / "calls.log"), **env}
    return subprocess.run([BASH, str(SCRIPT)], env=full_env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


@pytest.fixture
def stack(tmp_path: Path) -> Path:
    media = tmp_path / "media"
    media.mkdir()
    compose = media / "docker-compose.yml"
    shutil.copy(ROOT / "tests" / "fixtures" / "media-compose.yml", compose)
    return compose


def test_adds_the_service_once_and_starts_it(tmp_path: Path, stack: Path) -> None:
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    data = yaml.safe_load(stack.read_text(encoding="utf-8"))
    svc = data["services"]["telegram-downloader"]
    assert svc["environment"]["PUID"] == "1000" and svc["environment"]["TZ"] == "Europe/Madrid"   # from &env
    assert svc["environment"]["TGDL_STORAGE_SENTINEL"] == "/data/.tgdl-nas"
    assert svc["volumes"][1].endswith(":/data") and svc["build"] == "./src/telegram-downloader"
    assert set(data["services"]) >= {"sonarr", "radarr", "3cat-downloader", "seerr"}   # nothing else lost
    assert (tmp_path / "Plex" / ".tgdl-nas").exists()
    assert list(stack.parent.glob("docker-compose.yml.bak-*"))
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "git clone" in calls and "build --pull" in calls and "up -d --no-deps telegram-downloader" in calls
    for line in calls.splitlines():          # build/up only ever name our service: the rest of the stack keeps running
        if line.startswith("docker compose") and (" build " in line or " up " in line):
            assert line.endswith(" telegram-downloader"), line

    again = run(tmp_path)                     # second run = update, never a duplicate
    assert again.returncode == 0, again.stderr
    assert stack.read_text(encoding="utf-8").count("  telegram-downloader:") == 1
    assert "git -C" in (tmp_path / "calls.log").read_text(encoding="utf-8")


def test_refuses_when_the_nas_is_not_mounted(tmp_path: Path, stack: Path) -> None:
    before = stack.read_text(encoding="utf-8")
    result = run(tmp_path, STUB_MOUNTED="0")
    assert result.returncode != 0 and "no està muntat" in result.stderr
    assert stack.read_text(encoding="utf-8") == before
    assert not (tmp_path / "Plex" / ".tgdl-nas").exists()
