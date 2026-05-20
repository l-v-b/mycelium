#!/usr/bin/env python3
"""Verify mycelium drawer search quality vs the old mempalace search.

Runs a fixed set of queries through both:
  - mycelium-search   (new disk-first vault, BM25+vector hybrid)
  - mempalace-mempalace-search  (old mempalace, source of truth)

Compares top-N drawer ID overlap and prints a report. Use this to decide
whether the mempalace container can be safely decommissioned.

Both calls go through ContextForge so the comparison reflects what the
agent actually sees in real use.

Usage:
  CONTEXTFORGE_URL=http://nixliam.tail96a95d.ts.net:8000 \\
  CONTEXTFORGE_TOKEN=... \\
  SERVER_ID=e86ab056cea948c3b8ac28e0e1ca2199 \\
  python3 scripts/verify_search_migration.py [--queries queries.json] [--top 5]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

DEFAULT_QUERIES = [
    # Recent project topics
    "mycelium rebuild architecture decision",
    "ContextForge tool refresh procedure",
    "Phase 1 personal fork disk-first vault",
    "ChromaDB performance HNSW bottleneck",
    # Rain infrastructure
    "rain vibe kubernetes deployment",
    "openbao vault secret rotation",
    "argocd applications repo structure",
    "common-deployment helm chart",
    # Whelmed game
    "whelmed reward screen design",
    "whelmed card hand deck system",
    # Fitness / CityRock
    "cityrock climbing session pain tracking",
    "daily check-in sheet structure",
    # Memory system meta
    "mempalace drawer storage layout",
    "knowledge graph triples temporal",
    "mycelium typed links relations",
    # Tooling
    "claude code MCP config http",
    "google sheets oauth credentials",
    # Edge cases
    "zabbix",  # single token
    "how do I configure traefik ingress with letsencrypt cluster issuer",  # long natural query
]


def call_mcp_tool(cf_url: str, server_id: str, token: str, tool_name: str, args: dict, timeout: float = 30) -> tuple[dict | None, float]:
    """POST to ContextForge MCP endpoint, return (result_dict, elapsed_seconds).

    Returns (None, elapsed) on error.
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args},
    }).encode()
    req = Request(
        f"{cf_url}/servers/{server_id}/mcp/",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    t0 = time.time()
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except (URLError, HTTPError, TimeoutError) as e:
        return None, time.time() - t0

    elapsed = time.time() - t0
    # Handle SSE-wrapped responses
    if raw.lstrip().startswith("event:") or "\ndata:" in raw:
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip()), elapsed
                except json.JSONDecodeError:
                    continue
        return None, elapsed
    try:
        return json.loads(raw), elapsed
    except json.JSONDecodeError:
        return None, elapsed


def _fingerprint(text: str) -> str:
    """Stable identity for a search hit. Hash of first 200 chars after
    normalizing whitespace + case. Works across mycelium/mempalace because
    both return the same underlying drawer content.
    """
    import hashlib
    norm = " ".join((text or "")[:400].lower().split())[:200]
    return hashlib.sha256(norm.encode()).hexdigest()[:12]


def extract_hits(mcp_result: dict | None) -> list[tuple[str, str]]:
    """Pull (fingerprint, snippet) for each result.

    Both mycelium-search and mempalace-mempalace-search nest a JSON-encoded
    string in result.content[].text. We parse that and fingerprint each hit
    by its text content so the comparison works across the two systems'
    different schemas (drawer_id vs source_file vs chunk_index).
    """
    if not mcp_result:
        return []
    contents = mcp_result.get("result", {}).get("content", [])
    text = next((c.get("text", "") for c in contents if c.get("type") == "text"), "")
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []

    out = []
    for hit in parsed.get("results", []):
        body = hit.get("text", "")
        if not body:
            continue
        out.append((_fingerprint(body), body[:80].replace("\n", " ")))
    return out


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", help="JSON file with a list of query strings")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cf_url = os.environ.get("CONTEXTFORGE_URL", "").rstrip("/")
    token = os.environ.get("CONTEXTFORGE_TOKEN", "")
    server_id = os.environ.get("SERVER_ID", "")
    if not all([cf_url, token, server_id]):
        print("ERROR: set CONTEXTFORGE_URL, CONTEXTFORGE_TOKEN, SERVER_ID env vars", file=sys.stderr)
        sys.exit(2)

    queries = DEFAULT_QUERIES
    if args.queries:
        with open(args.queries) as f:
            queries = json.load(f)

    print(f"Comparing mycelium-search vs mempalace-mempalace-search on {len(queries)} queries (top {args.top})\n")
    print(f"{'#':>3} {'query':<55} {'myc':>4} {'mem':>4} {'jac':>5} {'myc(s)':>7} {'mem(s)':>7}")
    print("-" * 100)

    total_jac = 0.0
    total_myc_t = 0.0
    total_mem_t = 0.0
    valid = 0

    for i, q in enumerate(queries, 1):
        myc_res, myc_t = call_mcp_tool(
            cf_url, server_id, token,
            "mycelium-search", {"query": q, "n_results": args.top, "max_distance": 0.85},
        )
        mem_res, mem_t = call_mcp_tool(
            cf_url, server_id, token,
            "mempalace-mempalace-search", {"query": q, "limit": args.top, "max_distance": 0.85},
        )

        myc_hits = extract_hits(myc_res)
        mem_hits = extract_hits(mem_res)
        myc_fps = [fp for fp, _ in myc_hits]
        mem_fps = [fp for fp, _ in mem_hits]
        j = jaccard(myc_fps, mem_fps)

        print(f"{i:>3} {q[:55]:<55} {len(myc_hits):>4} {len(mem_hits):>4} {j:>5.2f} {myc_t:>7.2f} {mem_t:>7.2f}")

        if args.verbose:
            print(f"    myc top hits:")
            for fp, snip in myc_hits:
                print(f"      {fp}  {snip}")
            print(f"    mem top hits:")
            for fp, snip in mem_hits:
                print(f"      {fp}  {snip}")

        if myc_hits or mem_hits:
            total_jac += j
            total_myc_t += myc_t
            total_mem_t += mem_t
            valid += 1

    print("-" * 100)
    if valid:
        print(f"Avg Jaccard (top-{args.top} overlap): {total_jac/valid:.3f}")
        print(f"Avg latency mycelium: {total_myc_t/valid:.2f}s, mempalace: {total_mem_t/valid:.2f}s")
    print(f"\nQueries with results from both: {valid}/{len(queries)}")


if __name__ == "__main__":
    main()
