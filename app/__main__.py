"""OpsAgents: start the whole platform with one command.

    python -m app                 # everything on this machine, no Docker (fresh database)
    python -m app --docker        # use DATABASE_URL / OPA_URL from .env (docker compose up -d)
    python -m app --port 8080 --no-browser

Starts PostgreSQL + OPA (embedded mode), applies migrations, then runs in the
background: the guardrail (decides every proposal), the orchestrator (routes triggers,
waits for approvals, executes) and the GitHub watcher (commits, CI, pull requests),
and serves the dashboard at http://localhost:8080. Ctrl+C stops everything cleanly.
"""
import argparse
import os
import signal
import threading
import time
import webbrowser

from dotenv import load_dotenv


def seed_knowledge_base() -> None:
    """Give the RCA agent something to cite in a fresh database (first run downloads the
    small embedding model). Runs in the background so the app starts at once."""
    try:
        from agents.diagnostic_rca.seed_dev_kb import DOCS
        from shared import knowledge
        if knowledge.count() == 0:
            knowledge.add_documents([{"id": d[0], "source": d[0], "content": d[1]} for d in DOCS])
            print(f"[app] knowledge base seeded with {len(DOCS)} sample documents")
    except Exception as e:  # noqa: BLE001 - retrieval then just runs without documents
        print(f"[app] could not seed the knowledge base: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docker", action="store_true", help="use the services in .env instead of embedded ones")
    parser.add_argument("--port", type=int, default=int(os.getenv("APP_PORT", "8080")))
    parser.add_argument("--host", default=os.getenv("APP_HOST", "127.0.0.1"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-seed", action="store_true", help="don't add sample documents to the knowledge base")
    args = parser.parse_args()
    load_dotenv()

    stoppers = []
    if args.docker:
        from shared.migrate import migrate
        migrate(os.environ["DATABASE_URL"])
        os.environ["KNOWLEDGE_COLLECTION"] = os.getenv("KNOWLEDGE_COLLECTION", "devops_kb")
    else:
        from shared.devstack import start_embedded
        stoppers = start_embedded("opsagents-pg-")
        os.environ["KNOWLEDGE_COLLECTION"] = "devops_kb"

    # Imported only now: these read DATABASE_URL / OPA_URL at import time.
    import uvicorn

    from agents.guardrail import agent as guardrail
    from agents.orchestrator import github
    from agents.orchestrator.engine import Orchestrator
    from app.api import create_app

    stop = threading.Event()
    orchestrator = Orchestrator()
    orchestrator.recover()

    last_sweep = [0.0]

    def sweep_tick():
        if time.monotonic() - last_sweep[0] > 10:
            orchestrator.sweep()
            last_sweep[0] = time.monotonic()

    threads = [
        threading.Thread(target=guardrail.run, kwargs={"stop": stop}, name="guardrail", daemon=True),
        threading.Thread(target=orchestrator.consumer().run, kwargs={"stop": stop, "tick": sweep_tick},
                         name="orchestrator", daemon=True),
        threading.Thread(target=github.poll_forever, args=(orchestrator, stop), name="github", daemon=True),
    ]
    if not args.no_seed:
        threads.append(threading.Thread(target=seed_knowledge_base, name="seed", daemon=True))
    for t in threads:
        t.start()

    url = f"http://localhost:{args.port}"
    print(f"\n  OpsAgents is running: {url}\n  API docs: {url}/docs\n  Ctrl+C to stop\n")
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    try:
        uvicorn.run(create_app(orchestrator), host=args.host, port=args.port, log_level="warning")
    finally:
        stop.set()
        orchestrator.pool.shutdown(wait=False, cancel_futures=True)
        for fn in stoppers:
            fn()
        print("[app] stopped")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    main()
