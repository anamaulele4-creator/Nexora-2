"""Memória semântica do DÁRIO: PostgreSQL + pgvector, embeddings gerados no próprio Ollama."""

from __future__ import annotations

import json

import asyncpg
import httpx

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS dario_memories (
    id BIGSERIAL PRIMARY KEY,
    namespace TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding vector(%(dim)d) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dario_memories_ns ON dario_memories (namespace);
CREATE INDEX IF NOT EXISTS dario_memories_hnsw ON dario_memories USING hnsw (embedding vector_cosine_ops);
"""


def _vec(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.7g}" for v in values) + "]"


class Memory:
    def __init__(self, pool: asyncpg.Pool, http: httpx.AsyncClient, ollama_url: str, model: str, dim: int):
        self.pool, self.http, self.ollama_url, self.model, self.dim = pool, http, ollama_url, model, dim

    @classmethod
    async def connect(cls, dsn: str, http: httpx.AsyncClient, ollama_url: str, model: str, dim: int) -> "Memory":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA % {"dim": dim})
        return cls(pool, http, ollama_url, model, dim)

    async def close(self) -> None:
        await self.pool.close()

    def _prefixed(self, text: str, kind: str) -> str:
        # nomic-embed-text foi treinado com prefixos de tarefa; sem eles a pesquisa perde qualidade
        if "nomic-embed" in self.model:
            return f"{'search_query' if kind == 'query' else 'search_document'}: {text}"
        return text

    async def embed(self, text: str, kind: str = "document") -> list[float]:
        resp = await self.http.post("/api/embed", json={"model": self.model, "input": self._prefixed(text, kind)})
        resp.raise_for_status()
        vec = resp.json()["embeddings"][0]
        if len(vec) != self.dim:
            raise ValueError(f"O modelo {self.model} devolveu {len(vec)} dimensões; DARIO_EMBED_DIM={self.dim}")
        return vec

    async def add(self, namespace: str, content: str, metadata: dict) -> int:
        vec = await self.embed(content)
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO dario_memories (namespace, content, metadata, embedding) "
                "VALUES ($1, $2, $3::jsonb, $4::vector) RETURNING id",
                namespace, content, json.dumps(metadata), _vec(vec))

    async def search(self, namespace: str, query: str, k: int) -> list[dict]:
        vec = await self.embed(query, "query")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, content, metadata::text AS metadata, created_at, "
                "1 - (embedding <=> $2::vector) AS score FROM dario_memories "
                "WHERE namespace = $1 ORDER BY embedding <=> $2::vector LIMIT $3",
                namespace, _vec(vec), k)
        return [{"id": r["id"], "content": r["content"], "metadata": json.loads(r["metadata"]),
                 "score": round(float(r["score"]), 4), "created_at": r["created_at"].isoformat()} for r in rows]

    async def delete(self, namespace: str, memory_id: int) -> bool:
        async with self.pool.acquire() as conn:
            res = await conn.execute("DELETE FROM dario_memories WHERE namespace = $1 AND id = $2",
                                     namespace, memory_id)
        return res.endswith("1")
