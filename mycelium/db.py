"""pgvector storage backend — ChromaDB-compatible Collection shim.

Replaces the embedded/HTTP ChromaDB backend (old chroma.py) with PostgreSQL +
pgvector, for BOTH personal and team deployment modes. The markdown vault on
disk remains canonical; this is a derived, rebuildable index.

Design
------
Every call site in the codebase talks to a ChromaDB ``Collection`` via a tiny
method surface — ``.count() / .delete() / .query() / .upsert() / .get()`` — so
rather than rewrite those sites we reimplement that surface over pgvector. Each
of the four logical collections (notes, drawers, links, closets) is one table.

Schema follows the migration design note's principle: typed columns ONLY for
fields used in WHERE filters (drawers.wing/room; notes.status for the future
list-todos filter), everything else round-tripped through a JSONB ``extra``
column so the shim faithfully preserves whatever metadata the callers set.
``document`` holds the embedded text; ``embedding`` is the 384-dim MiniLM
vector (computed CLIENT-SIDE via mycelium.embedder — pgvector does not embed);
``body_tsv`` is a generated tsvector kept for future SQL-side FTS (today the
hybrid BM25 re-rank still runs in Python in search.py, unchanged).

Distances are cosine (``<=>``), identical semantics to the old
``hnsw:space=cosine`` ChromaDB collections, so search.py's distance→similarity
math and the dedupe threshold carry over unchanged.

Connection: a small psycopg connection pool sized for the per-role connection
limit (sit-pg-cluster caps mycelium_user at 10 concurrent). Synchronous writes
in both modes — no Redis, no writer worker.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from mycelium import config
from mycelium.embedder import embed

EMBED_DIM = 384

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()
_schema_ready = False


# --------------------------------------------------------------------------- #
# Connection pool
# --------------------------------------------------------------------------- #
def get_pool() -> ConnectionPool:
    """Lazily build the process-wide connection pool.

    Sized small to fit the per-role connection limit (mycelium_user = 10 on
    sit-pg-cluster). Override via MYCELIUM_DB_POOL_SIZE / MYCELIUM_DB_MAX_OVERFLOW.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=config.DATABASE_URL,
                    min_size=config.DB_POOL_MIN,
                    max_size=config.DB_POOL_SIZE,
                    timeout=config.DB_POOL_TIMEOUT,
                    max_idle=config.DB_POOL_RECYCLE,
                    kwargs={"row_factory": dict_row, "autocommit": True},
                    open=True,
                )
    return _pool


def get_client():
    """Compat shim — the old chroma.py exposed get_client(). Returns the pool.

    Also ensures the schema exists on first use, so a fresh database is
    initialised transparently (mirrors ChromaDB's get_or_create semantics).
    """
    pool = get_pool()
    _ensure_schema(pool)
    return pool


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
# Postgres caps a single tsvector at 1 MiB (1048575 bytes); to_tsvector raises
# ProgramLimitExceeded above that. Some documents exceed it — notably closet
# documents, which concatenate every drawer in a (wing, room) (the liam/general
# room alone is ~86k drawers → multi-MB). We cap the FULL-TEXT index input with
# left(document, 1000000); the complete document is still stored in `document`
# and embedded for vector search, so only the lexical index is truncated — and
# lexical search over a 1 MB+ blob is meaningless anyway. left() and the 2-arg
# to_tsvector(regconfig, text) are both immutable, so the expression stays valid
# in a STORED generated column.
_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS mycelium_notes (
    id        TEXT PRIMARY KEY,
    document  TEXT NOT NULL,
    embedding vector({dim}) NOT NULL,
    status    TEXT,
    extra     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    body_tsv  tsvector GENERATED ALWAYS AS (to_tsvector('english', left(document, 1000000))) STORED
);
CREATE INDEX IF NOT EXISTS mycelium_notes_embedding_idx
    ON mycelium_notes USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS mycelium_notes_tsv_idx
    ON mycelium_notes USING gin (body_tsv);
CREATE INDEX IF NOT EXISTS mycelium_notes_status_idx
    ON mycelium_notes (status) WHERE status IS NOT NULL;

CREATE TABLE IF NOT EXISTS mycelium_drawers (
    id        TEXT PRIMARY KEY,
    document  TEXT NOT NULL,
    embedding vector({dim}) NOT NULL,
    wing      TEXT,
    room      TEXT,
    extra     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    body_tsv  tsvector GENERATED ALWAYS AS (to_tsvector('english', left(document, 1000000))) STORED
);
CREATE INDEX IF NOT EXISTS mycelium_drawers_embedding_idx
    ON mycelium_drawers USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS mycelium_drawers_tsv_idx
    ON mycelium_drawers USING gin (body_tsv);
CREATE INDEX IF NOT EXISTS mycelium_drawers_wing_room_idx
    ON mycelium_drawers (wing, room);

CREATE TABLE IF NOT EXISTS mycelium_links (
    id        TEXT PRIMARY KEY,
    document  TEXT NOT NULL,
    embedding vector({dim}) NOT NULL,
    extra     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    body_tsv  tsvector GENERATED ALWAYS AS (to_tsvector('english', left(document, 1000000))) STORED
);
CREATE INDEX IF NOT EXISTS mycelium_links_embedding_idx
    ON mycelium_links USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS mycelium_links_tsv_idx
    ON mycelium_links USING gin (body_tsv);

CREATE TABLE IF NOT EXISTS mycelium_closets (
    id        TEXT PRIMARY KEY,
    document  TEXT NOT NULL,
    embedding vector({dim}) NOT NULL,
    extra     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    body_tsv  tsvector GENERATED ALWAYS AS (to_tsvector('english', left(document, 1000000))) STORED
);
CREATE INDEX IF NOT EXISTS mycelium_closets_embedding_idx
    ON mycelium_closets USING hnsw (embedding vector_cosine_ops);
""".format(dim=EMBED_DIM)


def init_schema() -> None:
    """Create the extension, tables and indexes if absent. Idempotent."""
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(_DDL)
    global _schema_ready
    _schema_ready = True


def _ensure_schema(pool: ConnectionPool) -> None:
    global _schema_ready
    if not _schema_ready:
        with _pool_lock:
            if not _schema_ready:
                with pool.connection() as conn:
                    conn.execute(_DDL)
                _schema_ready = True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _vec_literal(vec: list[float]) -> str:
    """pgvector text literal: '[0.1,0.2,...]'. Bound as text + cast ::vector,
    so no pgvector-python dependency is needed for I/O."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


# --------------------------------------------------------------------------- #
# Collection shim
# --------------------------------------------------------------------------- #
class Collection:
    """A ChromaDB-Collection-compatible view over one pgvector table.

    typed_cols maps metadata-key -> table column for the fields promoted out of
    the JSONB ``extra`` (those used in WHERE filters). On write, those keys are
    split into their columns; every other metadata key is stored in ``extra``.
    On read, the metadata dict is reassembled (columns ∪ extra) so callers see
    exactly what they wrote — a faithful ChromaDB round-trip.
    """

    def __init__(self, table: str, typed_cols: dict[str, str]):
        self.table = table
        self.typed_cols = typed_cols

    # -- internal --------------------------------------------------------------
    def _pool(self) -> ConnectionPool:
        pool = get_pool()
        _ensure_schema(pool)
        return pool

    def _split_metadata(self, meta: dict | None) -> tuple[dict, dict]:
        """Return (column_values, extra_dict)."""
        meta = dict(meta or {})
        cols: dict[str, Any] = {}
        for key, column in self.typed_cols.items():
            if key in meta:
                cols[column] = meta.pop(key)
        return cols, meta

    def _row_to_metadata(self, row: dict) -> dict:
        """Reassemble the caller-facing metadata dict from columns + extra."""
        meta = dict(row.get("extra") or {})
        for key, column in self.typed_cols.items():
            val = row.get(column)
            if val is not None:
                meta[key] = val
        return meta

    # -- count -----------------------------------------------------------------
    def count(self) -> int:
        with self._pool().connection() as conn:
            cur = conn.execute(f"SELECT count(*) AS n FROM {self.table}")
            return int(cur.fetchone()["n"])

    # -- delete ----------------------------------------------------------------
    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._pool().connection() as conn:
            conn.execute(f"DELETE FROM {self.table} WHERE id = ANY(%s)", [list(ids)])

    # -- upsert ----------------------------------------------------------------
    def upsert(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict] | None = None,
        embeddings: list[list[float]] | None = None,
    ) -> None:
        """Insert-or-update rows. Embeddings are computed client-side from
        ``documents`` when not supplied (ChromaDB embedded them server-side)."""
        if not ids:
            return
        metadatas = metadatas or [{} for _ in ids]
        if embeddings is None:
            embeddings = [embed(doc) for doc in documents]

        typed_columns = list(self.typed_cols.values())
        col_list = ["id", "document", "embedding", *typed_columns, "extra"]
        placeholders = (
            ["%s", "%s", "%s::vector"]
            + ["%s"] * len(typed_columns)
            + ["%s::jsonb"]
        )
        update_cols = ["document", "embedding", *typed_columns, "extra"]
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        sql = (
            f"INSERT INTO {self.table} ({', '.join(col_list)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"ON CONFLICT (id) DO UPDATE SET {set_clause}"
        )

        with self._pool().connection() as conn:
            with conn.cursor() as cur:
                for _id, doc, meta, emb in zip(ids, documents, metadatas, embeddings):
                    col_vals, extra = self._split_metadata(meta)
                    params = [_id, doc, _vec_literal(emb)]
                    params += [col_vals.get(c) for c in typed_columns]
                    params.append(json.dumps(extra))
                    cur.execute(sql, params)

    # -- get -------------------------------------------------------------------
    def get(
        self,
        ids: list[str] | None = None,
        where: dict | None = None,
        include: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict:
        """Fetch rows by id and/or metadata filter (or page through all).
        Returns ChromaDB-shaped FLAT lists:
        {"ids": [...], "documents": [...], "metadatas": [...]}."""
        include = include or []
        want_docs = "documents" in include
        want_meta = "metadatas" in include

        select_cols = ["id"]
        if want_docs:
            select_cols.append("document")
        if want_meta:
            select_cols += ["extra", *self.typed_cols.values()]

        conds: list[str] = []
        params: list[Any] = []
        if ids is not None:
            conds.append("id = ANY(%s)")
            params.append(list(ids))
        where_sql, where_params = self._build_where(where)
        if where_sql:
            conds.append(where_sql)
            params += where_params

        sql = f"SELECT {', '.join(select_cols)} FROM {self.table}"
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        if offset is not None:
            sql += " OFFSET %s"
            params.append(offset)

        with self._pool().connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        out: dict[str, list] = {"ids": [r["id"] for r in rows]}
        if want_docs:
            out["documents"] = [r.get("document") for r in rows]
        if want_meta:
            out["metadatas"] = [self._row_to_metadata(r) for r in rows]
        return out

    # -- query -----------------------------------------------------------------
    def query(
        self,
        query_texts: list[str],
        n_results: int = 10,
        where: dict | None = None,
        include: list[str] | None = None,
    ) -> dict:
        """Nearest-neighbour search. ``query_texts`` is embedded client-side and
        ranked by cosine distance. Returns ChromaDB-shaped NESTED lists (one
        inner list per query): {"ids":[[...]], "documents":[[...]],
        "metadatas":[[...]], "distances":[[...]]}."""
        include = include or ["documents", "metadatas", "distances"]
        query_text = query_texts[0] if query_texts else ""
        qvec = _vec_literal(embed(query_text))

        where_sql, where_params = self._build_where(where)

        select_cols = ["id", "embedding <=> %s::vector AS distance"]
        if "documents" in include:
            select_cols.append("document")
        if "metadatas" in include:
            select_cols += ["extra", *self.typed_cols.values()]

        sql = f"SELECT {', '.join(select_cols)} FROM {self.table}"
        params: list[Any] = [qvec]
        if where_sql:
            sql += f" WHERE {where_sql}"
            params += where_params
        sql += " ORDER BY distance ASC LIMIT %s"
        params.append(n_results)

        with self._pool().connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        ids = [r["id"] for r in rows]
        out: dict[str, list] = {"ids": [ids]}
        if "documents" in include:
            out["documents"] = [[r.get("document") for r in rows]]
        if "metadatas" in include:
            out["metadatas"] = [[self._row_to_metadata(r) for r in rows]]
        if "distances" in include:
            out["distances"] = [[float(r["distance"]) for r in rows]]
        return out

    def _build_where(self, where: dict | None) -> tuple[str, list]:
        """Translate the small subset of ChromaDB where-filters the codebase
        uses ({"k": v} and {"$and": [...]}) into SQL against typed columns or
        the JSONB extra. Returns (sql_fragment, params)."""
        if not where:
            return "", []

        if "$and" in where:
            frags, params = [], []
            for clause in where["$and"]:
                f, p = self._build_where(clause)
                if f:
                    frags.append(f)
                    params += p
            return " AND ".join(frags), params

        # single {key: value}
        frags, params = [], []
        for key, value in where.items():
            if key in self.typed_cols:
                frags.append(f"{self.typed_cols[key]} = %s")
                params.append(value)
            else:
                frags.append("extra->>%s = %s")
                params += [key, value]
        return " AND ".join(frags), params


# --------------------------------------------------------------------------- #
# Collection factories (same names the old chroma.py exported)
# --------------------------------------------------------------------------- #
def notes_collection() -> Collection:
    return Collection("mycelium_notes", typed_cols={"status": "status"})


def drawers_collection() -> Collection:
    return Collection("mycelium_drawers", typed_cols={"wing": "wing", "room": "room"})


def links_collection() -> Collection:
    return Collection("mycelium_links", typed_cols={})


def closets_collection() -> Collection:
    return Collection("mycelium_closets", typed_cols={})
