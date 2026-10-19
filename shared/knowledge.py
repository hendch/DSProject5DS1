"""Knowledge base on PostgreSQL + pgvector (db/migrations/005_knowledge.sql).

Documents are stored with an embedding computed locally by fastembed
(BAAI/bge-small-en-v1.5, 384 dimensions, CPU, downloaded once); search ranks them by
cosine distance (0 = same meaning, 2 = opposite). Replaces Chroma.

    add_documents([{"id": "pg-not-ready", "source": "...", "content": "..."}])
    search(["connection refused on localhost:5432"]) -> [{id, source, text, distance}]
"""
import json
import os
from typing import Callable, Iterable

import psycopg2.extras
from dotenv import load_dotenv

from shared.db import get_connection

load_dotenv()
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
DIMENSIONS = 384  # must match knowledge.documents.embedding
DEFAULT_COLLECTION = os.getenv("KNOWLEDGE_COLLECTION", "devops_kb")

_embedder: Callable[[list[str]], list[list[float]]] | None = None
_model = None


def _fastembed(texts: list[str]) -> list[list[float]]:
    from fastembed import TextEmbedding  # imported lazily: loads the model on first use

    global _model
    if _model is None:
        _model = TextEmbedding(EMBEDDING_MODEL)
    return [list(map(float, v)) for v in _model.embed(texts)]


def set_embedder(fn: Callable[[list[str]], list[list[float]]] | None) -> None:
    """Swap the embedding function (tests use a cheap deterministic one)."""
    global _embedder
    _embedder = fn


def embed(texts: list[str]) -> list[list[float]]:
    vectors = (_embedder or _fastembed)(list(texts))
    for v in vectors:
        if len(v) != DIMENSIONS:
            raise ValueError(f"embedding has {len(v)} dimensions, the database expects {DIMENSIONS}")
    return vectors


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"


def add_documents(docs: Iterable[dict], collection: str = DEFAULT_COLLECTION, use_shared=False) -> int:
    """Insert or replace documents: each {id, source, content, metadata?}. Returns how many."""
    docs = list(docs)
    if not docs:
        return 0
    vectors = embed([d["content"] for d in docs])
    conn = get_connection(use_shared)
    try:
        with conn, conn.cursor() as cur:
            for d, v in zip(docs, vectors):
                cur.execute(
                    """INSERT INTO knowledge.documents (collection, id, source, content, metadata, embedding)
                       VALUES (%s, %s, %s, %s, %s, %s::vector)
                       ON CONFLICT (collection, id) DO UPDATE SET source = EXCLUDED.source,
                         content = EXCLUDED.content, metadata = EXCLUDED.metadata,
                         embedding = EXCLUDED.embedding""",
                    (collection, d["id"], d.get("source") or d["id"], d["content"],
                     json.dumps(d.get("metadata") or {}), _literal(v)))
    finally:
        conn.close()
    return len(docs)


def count(collection: str = DEFAULT_COLLECTION, use_shared=False) -> int:
    conn = get_connection(use_shared)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM knowledge.documents WHERE collection = %s", (collection,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def search(queries: list[str] | str, k: int = 3, limit: int = 4, max_distance: float = 2.0,
           collection: str = DEFAULT_COLLECTION, max_chars: int = 600, use_shared=False) -> list[dict]:
    """Run several queries, keep each document's best (lowest) distance, closest first."""
    if isinstance(queries, str):
        queries = [queries]
    best: dict[str, dict] = {}
    conn = get_connection(use_shared)
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for v in embed(queries):
                cur.execute(
                    """SELECT id, source, content, embedding <=> %s::vector AS distance
                       FROM knowledge.documents WHERE collection = %s
                       ORDER BY embedding <=> %s::vector LIMIT %s""",
                    (_literal(v), collection, _literal(v), k))
                for r in cur.fetchall():
                    dist = round(float(r["distance"]), 3)
                    if r["id"] not in best or dist < best[r["id"]]["distance"]:
                        best[r["id"]] = {"id": r["id"], "source": r["source"],
                                         "text": r["content"][:max_chars], "distance": dist}
    finally:
        conn.close()
    ranked = sorted(best.values(), key=lambda d: d["distance"])
    return [d for d in ranked if d["distance"] <= max_distance][:limit]
