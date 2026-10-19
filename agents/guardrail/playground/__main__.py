"""Guardrail Playground: run the whole guardrail locally and drive it from a web page.

    python -m agents.guardrail.playground            # no Docker needed
    python -m agents.guardrail.playground --docker   # use docker-compose services from .env

Default mode starts everything in-process: a throwaway PostgreSQL 16 (pgserver,
every migration applied; it is also the event bus) and OPA with the real policy
(reloaded live when you edit the .rego file). Every launch starts from an empty
database. Needs: pip install -e ".[dev]" (from the repo root)

Then open http://localhost:8090
"""
import argparse
import threading
import webbrowser
from pathlib import Path

from shared.devstack import start_embedded


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docker", action="store_true", help="use the docker-compose services from .env")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    stoppers = [] if args.docker else start_embedded("guardrail-pg-")

    # Imported only now: these modules read DATABASE_URL / OPA_URL at import time.
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
