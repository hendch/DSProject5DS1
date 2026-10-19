"""Retrieval layer for the Diagnostic/RCA agent (the "R" in RAG).

Queries the Chroma collection that corpus/ingest.py (Member E) fills with
docs and the Stack Exchange subset. If Chroma is down or the collection is
empty, we return [] so the agent still runs without retrieved context.
"""
import os

import chromadb
from dotenv import load_dotenv

load_dotenv()
HOST = os.getenv("CHROMA_HOST", "localhost")
PORT = int(os.getenv("CHROMA_PORT", "8000"))
COLLECTION = os.getenv("CHROMA_COLLECTION", "devops_kb")  # must match corpus/ingest.py
MAX_DOC_CHARS = 600  # keep prompts small for a 3B model
# Chroma returns a distance per hit (lower = closer). Docs further than this are dropped.
# Tune it by watching the "[retrieval] candidates" line printed on every run.
MAX_DISTANCE = float(os.getenv("RETRIEVAL_MAX_DISTANCE", "1.2"))


def retrieve(queries: list[str] | str, k: int = 3, limit: int = 4) -> list[dict]:
    """Run one or more queries and merge the hits, keeping each doc's best distance.

    Several short queries (one per error line) match specific docs better than
    one long, noisy query. Returns up to `limit` docs as
    [{"id", "source", "text", "distance"}], closest first.
    """
    if isinstance(queries, str):
        queries = [queries]
    try:
        client = chromadb.HttpClient(host=HOST, port=PORT)
        collection = client.get_collection(COLLECTION)
        res = collection.query(query_texts=queries, n_results=k)
    except Exception as e:
        print(f"[warn] retrieval unavailable ({type(e).__name__}: {e}); continuing without context")
        return []

    best: dict[str, dict] = {}
    for qi in range(len(queries)):
        hits = zip(res["ids"][qi], res["documents"][qi], res["metadatas"][qi], res["distances"][qi])
        for doc_id, text, meta, dist in hits:
            meta = meta or {}
            dist = 0.0 if dist is None else float(dist)
            if doc_id not in best or dist < best[doc_id]["distance"]:
                best[doc_id] = {
                    "id": doc_id,
                    "source": str(meta.get("source") or meta.get("url") or doc_id),
                    "text": text[:MAX_DOC_CHARS],
                    "distance": round(dist, 2),
                }

    ranked = sorted(best.values(), key=lambda d: d["distance"])
    print(f"[retrieval] candidates (cutoff {MAX_DISTANCE}): {[(d['source'], d['distance']) for d in ranked]}")
    return [d for d in ranked if d["distance"] <= MAX_DISTANCE][:limit]