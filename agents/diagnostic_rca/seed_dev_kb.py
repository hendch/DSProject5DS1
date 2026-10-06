"""DEV ONLY: seeds a throwaway Chroma collection so retrieval can be tested
before Member E's real ingestion (corpus/ingest.py) exists.

Run:  python -m agents.diagnostic_rca.seed_dev_kb
Then set CHROMA_COLLECTION=devops_kb_dev in .env (the default collection
name in retrieval.py is devops_kb, which we leave free for Member E).
"""
import os

import chromadb
from dotenv import load_dotenv

load_dotenv()
HOST = os.getenv("CHROMA_HOST", "localhost")
PORT = int(os.getenv("CHROMA_PORT", "8000"))
NAME = os.getenv("CHROMA_COLLECTION", "devops_kb_dev")

# Placeholder notes written for testing only, not real documentation.
DOCS = [
    ("dev-seed-1", "Tests fail with 'connection refused' on localhost:5432 when the database service "
                   "container is not yet ready. Add a health check to the service and wait for it before running tests."),
    ("dev-seed-2", "If DATABASE_URL is not set, applications often fall back to a localhost default. In CI the "
                   "database is a separate service host, so the environment variable must be set explicitly."),
    ("dev-seed-3", "npm ERESOLVE errors mean a package's peer dependency range conflicts with an installed version. "
                   "Align versions or use a compatible release of the package."),
    ("dev-seed-4", "Terraform/OpenTofu 'Error acquiring the state lock' means another run holds the lock, or a "
                   "crashed run left a stale one. Confirm no run is active before using force-unlock."),
    ("dev-seed-5", "Docker build failing at 'pip install' with 'No matching distribution found' usually means the "
                   "pinned version does not exist or the base image's Python version is unsupported."),
]


if __name__ == "__main__":
    client = chromadb.HttpClient(host=HOST, port=PORT)
    collection = client.get_or_create_collection(NAME)
    collection.upsert(
        ids=[d[0] for d in DOCS],
        documents=[d[1] for d in DOCS],
        metadatas=[{"source": d[0]} for d in DOCS],
    )
    print(f"Seeded {len(DOCS)} docs into '{NAME}' at {HOST}:{PORT} (now {collection.count()} total)")