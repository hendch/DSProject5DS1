"""Guardrail Playground: run the whole guardrail locally and drive it from a web page.

    python -m agents.guardrail.playground            # no Docker needed
    python -m agents.guardrail.playground --docker   # use docker-compose services from .env

Default mode starts everything in-process: a throwaway PostgreSQL 16
(pgserver), an in-memory Redis server (fakeredis), and OPA with the real
policy (reloaded live when you edit the .rego file). Every launch starts
from an empty database. Needs: pip install -r requirements-dev.txt

Then open http://localhost:8090
"""
import argparse
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
POLICY_DIR = ROOT / "agents/guardrail/policy"
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
        print(f"[playground] downloading OPA {OPA_VERSION} from {url}")
        TOOLS.mkdir(exist_ok=True)
        urllib.request.urlretrieve(url, target)
        target.chmod(0o755)
    return str(target)


def start_embedded() -> list:
    """Postgres + Redis + OPA without Docker. Returns things to stop on exit."""
    import psycopg2
    import pgserver
    from fakeredis import TcpFakeServer

    print("[playground] starting PostgreSQL 16 (pgserver)...")
    pg = pgserver.get_server(tempfile.mkdtemp(prefix="guardrail-pg-"), cleanup_mode="stop")
    admin = pg.get_uri()
    conn = psycopg2.connect(admin)
    conn.autocommit = True
    conn.cursor().execute("CREATE DATABASE agents")
    conn.close()
    db_url = admin.rsplit("/", 1)[0] + "/agents"
    conn = psycopg2.connect(db_url)
    with conn, conn.cursor() as cur:
        for f in ("db/schema.sql", "db/migrations/002_guardrail.sql"):
            cur.execute((ROOT / f).read_text())
    conn.close()

    redis_port = free_port()
    redis_srv = TcpFakeServer(("127.0.0.1", redis_port), server_type="redis")
    threading.Thread(target=redis_srv.serve_forever, daemon=True).start()

    opa_port = free_port()
    opa = subprocess.Popen([opa_binary(), "run", "--server", f"--addr=127.0.0.1:{opa_port}",
                            "--watch", str(POLICY_DIR)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_for(f"http://127.0.0.1:{opa_port}/health", "OPA")

    os.environ.update({"DATABASE_URL": db_url, "REDIS_HOST": "127.0.0.1",
                       "REDIS_PORT": str(redis_port), "OPA_URL": f"http://127.0.0.1:{opa_port}",
                       "GUARDRAIL_USE_SHARED": "false"})
    return [opa.terminate, redis_srv.shutdown]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docker", action="store_true", help="use the docker-compose services from .env")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    stoppers = [] if args.docker else start_embedded()

    # Imported only now: these modules read DATABASE_URL / REDIS_* / OPA_URL at import time.
    import uvicorn
    from fastapi.responses import FileResponse

    from agents.guardrail import agent
    from agents.guardrail.api import app
    from agents.guardrail.playground import routes

    app.include_router(routes.router)
    app.add_api_route("/", lambda: FileResponse(Path(__file__).parent / "index.html"), include_in_schema=False)

    consumer = threading.Thread(target=agent.run, name="guardrail-consumer", daemon=True)
    consumer.start()
    routes.state["consumer"] = consumer

    url = f"http://localhost:{args.port}"
    print(f"\n[playground] ready: {url}   (API docs: {url}/docs)   Ctrl+C to stop\n")
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        for stop in stoppers:
            stop()


if __name__ == "__main__":
    main()
