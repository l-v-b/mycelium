"""mycelium-notes adapter. In-process search over the mycelium notes collection."""
from __future__ import annotations

from mycelium.federation.sources import SourceClient, SourceResult, register


class MyceliumNotesClient:
    name = "mycelium-notes"

    async def search(self, query: str, k: int) -> list[SourceResult]:
        # ChromaDB is sync; this adapter doesn't actually await anything
        # but its async signature lets fanout.py asyncio.gather it together
        # with truly-async external adapters.
        from mycelium.chroma import notes_collection
        hits = notes_collection().query(query_texts=[query], n_results=k)
        if not hits.get("ids") or not hits["ids"][0]:
            return []

        out: list[SourceResult] = []
        for i, note_id in enumerate(hits["ids"][0]):
            distance = hits["distances"][0][i] if hits.get("distances") else 1.0
            meta = (hits["metadatas"][0][i] if hits.get("metadatas") else {}) or {}
            document = hits["documents"][0][i] if hits.get("documents") else ""
            out.append(SourceResult(
                source=self.name,
                rank=max(0.0, 1.0 - float(distance)),
                id=note_id,
                title=meta.get("title"),
                snippet=(document or "")[:300].replace("\n", " "),
                metadata={
                    "tags": meta.get("tags", ""),
                    "status": meta.get("status", ""),
                    "filepath": meta.get("filepath", ""),
                },
            ))
        return out


register(MyceliumNotesClient())
