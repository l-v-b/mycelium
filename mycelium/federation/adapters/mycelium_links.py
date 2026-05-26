"""mycelium-links adapter. In-process search over the mycelium links collection."""
from __future__ import annotations

from mycelium.federation.sources import SourceClient, SourceResult, register


class MyceliumLinksClient:
    name = "mycelium-links"

    async def search(self, query: str, k: int) -> list[SourceResult]:
        from mycelium.chroma import links_collection
        hits = links_collection().query(query_texts=[query], n_results=k)
        if not hits.get("ids") or not hits["ids"][0]:
            return []

        out: list[SourceResult] = []
        for i, link_id in enumerate(hits["ids"][0]):
            distance = hits["distances"][0][i] if hits.get("distances") else 1.0
            meta = (hits["metadatas"][0][i] if hits.get("metadatas") else {}) or {}
            document = hits["documents"][0][i] if hits.get("documents") else ""
            title = f"{meta.get('source_label', '?')} --[{meta.get('relation_type', '?')}]--> {meta.get('target_label', '?')}"
            out.append(SourceResult(
                source=self.name,
                rank=max(0.0, 1.0 - float(distance)),
                id=link_id,
                title=title,
                snippet=(document or meta.get("description", ""))[:300].replace("\n", " "),
                metadata={
                    "source_id": meta.get("source_id", ""),
                    "target_id": meta.get("target_id", ""),
                    "relation_type": meta.get("relation_type", ""),
                    "ended_at": meta.get("ended_at", ""),
                },
            ))
        return out


register(MyceliumLinksClient())
