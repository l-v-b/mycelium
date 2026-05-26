"""mycelium-drawers adapter. In-process search over the mycelium drawers collection."""
from __future__ import annotations

from mycelium.federation.sources import SourceClient, SourceResult, register


class MyceliumDrawersClient:
    name = "mycelium-drawers"

    async def search(self, query: str, k: int) -> list[SourceResult]:
        from mycelium.chroma import drawers_collection
        hits = drawers_collection().query(query_texts=[query], n_results=k)
        if not hits.get("ids") or not hits["ids"][0]:
            return []

        out: list[SourceResult] = []
        for i, drawer_id in enumerate(hits["ids"][0]):
            distance = hits["distances"][0][i] if hits.get("distances") else 1.0
            meta = (hits["metadatas"][0][i] if hits.get("metadatas") else {}) or {}
            document = hits["documents"][0][i] if hits.get("documents") else ""
            out.append(SourceResult(
                source=self.name,
                rank=max(0.0, 1.0 - float(distance)),
                id=drawer_id,
                title=None,
                snippet=(document or "")[:300].replace("\n", " "),
                metadata={
                    "wing": meta.get("wing", ""),
                    "room": meta.get("room", ""),
                    "filed_at": meta.get("filed_at", ""),
                },
            ))
        return out


register(MyceliumDrawersClient())
