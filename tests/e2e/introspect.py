"""White-box introspection of a running stack via `docker exec`.

This is what makes a passing run hard to fake. Black-box MCP calls can only
observe what the server chooses to return — and we proved (by reading the
source) that the read path never surfaces `author`, `committed_at`, or
`source_intent`. To verify those were actually written, and to verify the
team-mode async lifecycle (disk draft -> Redis stream -> worker commit -> flag
flip), we must look at ground truth: the files on the pod's volume and the
Redis stream itself.

In the hermetic docker-compose harness we reach those by `docker exec` into
the named containers. Against a live k8s SIT deployment the same probes can be
pointed at `kubectl exec` by setting MYCELIUM_E2E_EXEC="kubectl exec -n <ns>".

If no server container/exec is configured, `Introspector.enabled` is False and
the white-box tests skip (the black-box tests still run everywhere).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Optional


class Introspector:
    def __init__(self) -> None:
        # Base exec command. Defaults to `docker exec`; override with
        # MYCELIUM_E2E_EXEC="kubectl exec -n mycelium-sit" for in-cluster.
        self.exec_base = os.environ.get("MYCELIUM_E2E_EXEC", "docker exec")
        self.server = os.environ.get("MYCELIUM_E2E_SERVER_CONTAINER", "")
        self.worker = os.environ.get("MYCELIUM_E2E_WORKER_CONTAINER", "")
        self.redis = os.environ.get("MYCELIUM_E2E_REDIS_CONTAINER", "")
        self.data_dir = os.environ.get("MYCELIUM_E2E_DATA_DIR", "/data")
        # kubectl needs `-- <cmd>`; docker does not. Detect heuristically.
        self._needs_dashdash = "kubectl" in self.exec_base

    @property
    def enabled(self) -> bool:
        return bool(self.server)

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis)

    # -- low level --------------------------------------------------------------

    def _exec(self, container: str, argv: list[str]) -> str:
        cmd = shlex.split(self.exec_base) + [container]
        if self._needs_dashdash:
            cmd += ["--"]
        cmd += argv
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(
                f"exec failed ({proc.returncode}): {' '.join(cmd)}\n"
                f"stdout={proc.stdout[:500]}\nstderr={proc.stderr[:500]}"
            )
        return proc.stdout

    def _py_in_server(self, snippet: str) -> str:
        return self._exec(self.server, ["python", "-c", snippet])

    # -- disk frontmatter -------------------------------------------------------

    def note_frontmatter(self, note_id: str) -> Optional[dict]:
        """Return the on-disk frontmatter dict of the note whose `id` matches,
        or None if no such file exists yet."""
        return self._frontmatter_by_field("notes", "id", note_id)

    def drawer_frontmatter(self, drawer_id: str) -> Optional[dict]:
        return self._frontmatter_by_field("drawers", "id", drawer_id)

    def _frontmatter_by_field(self, subdir: str, field: str, value: str) -> Optional[dict]:
        snippet = (
            "import json,glob,frontmatter,os;"
            f"d=os.path.join({self.data_dir!r},'vault',{subdir!r});"
            "out=None;\n"
            "for p in glob.glob(os.path.join(d,'*.md')):\n"
            "    try:\n"
            "        m=frontmatter.load(p)\n"
            f"        if str(m.get({field!r}))=={value!r}:\n"
            "            out=dict(m.metadata); out['__path__']=p; break\n"
            "    except Exception:\n"
            "        pass\n"
            "print(json.dumps(out, default=str))"
        )
        raw = self._py_in_server(snippet).strip()
        return json.loads(raw) if raw and raw != "null" else None

    def count_pending(self) -> int:
        """Count notes+drawers on disk with committed_at == null (team drafts
        awaiting the worker)."""
        snippet = (
            "import glob,frontmatter,os;"
            f"base=os.path.join({self.data_dir!r},'vault');"
            "n=0;\n"
            "for sub in ('notes','drawers'):\n"
            "    for p in glob.glob(os.path.join(base,sub,'*.md')):\n"
            "        try:\n"
            "            if frontmatter.load(p).get('committed_at') is None: n+=1\n"
            "        except Exception: pass\n"
            "print(n)"
        )
        return int(self._py_in_server(snippet).strip())

    # -- chroma index counts ----------------------------------------------------

    def chroma_counts(self) -> dict:
        """notes/drawers/links counts straight from ChromaDB inside the pod."""
        snippet = (
            "import json;"
            "from mycelium.chroma import notes_collection,drawers_collection,links_collection;"
            "print(json.dumps({'notes':notes_collection().count(),"
            "'drawers':drawers_collection().count(),"
            "'links':links_collection().count()}))"
        )
        return json.loads(self._py_in_server(snippet).strip())

    # -- redis stream -----------------------------------------------------------

    def stream_len(self, stream: str = "mycelium:writes") -> int:
        out = self._exec(self.redis, ["redis-cli", "XLEN", stream])
        return int(out.strip() or "0")
