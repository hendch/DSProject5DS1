"""Start the platform's services on this machine without Docker, for demos and dev:
a throwaway PostgreSQL 16 with pgvector (pgserver, every migration applied) and OPA
with the guardrail policy (reloaded live when the .rego files change).

    stops = start_embedded()     # sets DATABASE_URL and OPA_URL in os.environ
    ...
    for stop in stops: stop()

Every call starts from an empty database. Needs the dev extras: pip install -e ".[dev]"
"""
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "agents" / "guardrail" / "policy"
TOOLS = ROOT / ".tools"
OPA_VERSION = "1.20.2"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url: str, what: str, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except OSError:
            time.sleep(0.3)
    sys.exit(f"{what} did not start ({url})")


def opa_binary() -> str:
    """OPA_BIN, then opa on PATH, then .tools/, else download the pinned release."""
    found = os.getenv("OPA_BIN") or shutil.which("opa")
    if found:
        return found
    system, machine = platform.system().lower(), platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    name = {"windows": f"opa_windows_{arch}.exe", "darwin": f"opa_darwin_{arch}",
            "linux": f"opa_linux_{arch}_static"}[system]
    target = TOOLS / ("opa.exe" if system == "windows" else "opa")
    if not target.exists():
        url = f"https://github.com/open-policy-agent/opa/releases/download/v{OPA_VERSION}/{name}"
        print(f"[devstack] downloading OPA {OPA_VERSION} from {url}")
        TOOLS.mkdir(exist_ok=True)
        urllib.request.urlretrieve(url, target)
        target.chmod(0o755)
    return str(target)


def start_postgres(prefix: str = "agents-pg-") -> str:
    """A fresh PostgreSQL with every migration applied. Returns its URL."""
    import psycopg2
    import pgserver

    from shared.migrate import migrate

    print("[devstack] starting PostgreSQL 16 (pgserver)...")
    pg = pgserver.get_server(tempfile.mkdtemp(prefix=prefix), cleanup_mode="stop")
    admin = pg.get_uri()
    conn = psycopg2.connect(admin)
    conn.autocommit = True
    conn.cursor().execute("CREATE DATABASE agents")
    conn.close()
    db_url = admin.rsplit("/", 1)[0] + "/agents"
    migrate(db_url, quiet=True)
    return db_url


def start_opa() -> tuple[str, callable]:
    """OPA serving the guardrail policy. Returns (url, stop)."""
    port = free_port()
    proc = subprocess.Popen([opa_binary(), "run", "--server", f"--addr=127.0.0.1:{port}",
                             "--watch", str(POLICY_DIR)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    wait_for(f"{url}/health", "OPA")
    return url, proc.terminate


def start_embedded(prefix: str = "agents-pg-") -> list:
    """PostgreSQL + OPA. Points DATABASE_URL / OPA_URL at them; returns stop functions."""
    db_url = start_postgres(prefix)
    opa_url, stop_opa = start_opa()
    os.environ.update({"DATABASE_URL": db_url, "OPA_URL": opa_url, "GUARDRAIL_USE_SHARED": "false"})
    return [stop_opa]
