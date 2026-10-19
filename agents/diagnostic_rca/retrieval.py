"""Retrieval layer for the Diagnostic/RCA agent (the "R" in RAG).

Searches the knowledge base in PostgreSQL + pgvector (shared/knowledge.py), which
corpus/ingest.py (Member E) fills with docs and the Stack Exchange subset. If the
database is down or the collection is empty, we return [] so the agent still runs
without retrieved context.
"""
import os

from dotenv import load_dotenv

from shared import knowledge

load_dotenv()
COLLECTION = os.getenv("KNOWLEDGE_COLLECTION", "devops_kb")  # must match corpus/ingest.py
MAX_DOC_CHARS = 600  # keep prompts small for a small model
# Cosine distance per hit (0 = same meaning, 2 = opposite). Docs further than this are dropped.
# 0.3 measured on the dev seed with bge-small: relevant docs scored 0.20-0.22 against the
# sample failed build, unrelated ones 0.35 and up.
# Tune it by watching the "[retrieval] candidates" line printed on every run.
MAX_DISTANCE = float(os.getenv("RETRIEVAL_MAX_DISTANCE", "0.3"))


def retrieve(queries: list[str] | str, k: int = 3, limit: int = 4) -> list[dict]:
    """Run one or more queries and merge the hits, keeping each doc's best distance.

    Several short queries (one per error line) match specific docs better than
    one long, noisy query. Returns up to `limit` docs as
    [{"id", "source", "text", "distance"}], closest first.
    """
    try:
        ranked = knowledge.search(queries, k=k, limit=50, collection=COLLECTION, max_chars=MAX_DOC_CHARS)
    except Exception as e:  # noqa: BLE001 - database down, extension missing, model download failed
        print(f"[warn] retrieval unavailable ({type(e).__name__}: {e}); continuing without context")
        return []
    print(f"[retrieval] candidates (cutoff {MAX_DISTANCE}): {[(d['source'], d['distance']) for d in ranked]}")
    return [d for d in ranked if d["distance"] <= MAX_DISTANCE][:limit]
