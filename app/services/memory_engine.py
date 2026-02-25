from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from app.core.config import ROOT

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _stable_meta_str(metadata: dict[str, Any]) -> str:
    safe = {str(k): metadata[k] for k in sorted(metadata.keys())}
    return json.dumps(safe, ensure_ascii=True, sort_keys=True, default=str)


def _chunk_text(text: str, chunk_size: int = 800, overlap: int = 120) -> list[str]:
    src = str(text or "").strip()
    if not src:
        return []
    if len(src) <= chunk_size:
        return [src]
    out: list[str] = []
    step = max(80, chunk_size - max(0, overlap))
    i = 0
    n = len(src)
    while i < n:
        part = src[i : i + chunk_size].strip()
        if part:
            out.append(part)
        if i + chunk_size >= n:
            break
        i += step
    return out


class OnyxMemory:
    """Local vector memory for long-term recall (Chroma + sentence-transformers)."""

    _model_cache: Any = None

    def __init__(
        self,
        db_path: str | Path | None = None,
        collection_name: str = "onyx_knowledge",
        model_name: str = "all-MiniLM-L6-v2",
    ):
        self.collection_name = str(collection_name or "onyx_knowledge").strip() or "onyx_knowledge"
        self.model_name = str(model_name or "all-MiniLM-L6-v2").strip() or "all-MiniLM-L6-v2"
        self.db_path = Path(db_path or (ROOT / "onyx_data" / "memory_db")).resolve()
        self.db_path.mkdir(parents=True, exist_ok=True)

        self.available = False
        self._client = None
        self._collection = None
        self._model = None

        if str(os.getenv("INVESTOR_DISABLE_MEMORY", "0")).strip() in {"1", "true", "TRUE", "yes", "YES"}:
            logger.warning("OnyxMemory disabled by INVESTOR_DISABLE_MEMORY.")
            return

        # ChromaDB 0.5.x can emit noisy telemetry failures with newer posthog SDKs.
        # Keep memory enabled while turning telemetry capture into a no-op.
        try:
            import posthog  # type: ignore

            def _capture_noop(*_args: Any, **_kwargs: Any) -> None:
                return None

            posthog.capture = _capture_noop  # type: ignore[assignment]
            os.environ.setdefault("ANONYMIZED_TELEMETRY", "FALSE")
        except Exception:
            pass

        try:
            import chromadb  # type: ignore
            from chromadb.config import Settings  # type: ignore
            from sentence_transformers import SentenceTransformer  # type: ignore
        except Exception:
            logger.warning("OnyxMemory dependencies unavailable (chromadb/sentence-transformers).")
            return

        try:
            self._client = chromadb.PersistentClient(
                path=str(self.db_path),
                settings=Settings(anonymized_telemetry=False) if Settings is not None else None,
            )
            self._collection = self._client.get_or_create_collection(name=self.collection_name)
            if OnyxMemory._model_cache is None:
                OnyxMemory._model_cache = SentenceTransformer(self.model_name)
            self._model = OnyxMemory._model_cache
            self.available = True
        except Exception as exc:
            logger.exception("OnyxMemory init failed: %s", exc)
            self.available = False

    def _embed(self, text: str) -> list[float]:
        if not self.available or self._model is None:
            raise RuntimeError("OnyxMemory is unavailable.")
        emb = self._model.encode(str(text), normalize_embeddings=True)
        return [float(x) for x in emb.tolist()]

    def memorize(self, text: str, metadata: dict[str, Any]) -> int:
        """Persist memory chunks. Returns number of chunks upserted."""
        if not self.available or self._collection is None:
            return 0
        base_text = str(text or "").strip()
        if not base_text:
            return 0
        meta = dict(metadata or {})
        if not meta.get("timestamp"):
            meta["timestamp"] = _utc_now_iso()

        chunks = _chunk_text(base_text, chunk_size=800, overlap=120)
        if not chunks:
            return 0

        ids: list[str] = []
        docs: list[str] = []
        embs: list[list[float]] = []
        metas: list[dict[str, Any]] = []

        # Keep deterministic id material independent from auto timestamp.
        id_material_meta = {k: v for k, v in meta.items() if k != "timestamp"}
        id_meta_s = _stable_meta_str(id_material_meta)

        for idx, chunk in enumerate(chunks):
            h = hashlib.sha256()
            h.update(id_meta_s.encode("utf-8", errors="ignore"))
            h.update(b"|")
            h.update(str(idx).encode("utf-8"))
            h.update(b"|")
            h.update(chunk.encode("utf-8", errors="ignore"))
            mem_id = h.hexdigest()
            row_meta = dict(meta)
            row_meta["chunk_index"] = idx
            row_meta["chunk_count"] = len(chunks)
            ids.append(mem_id)
            docs.append(chunk)
            embs.append(self._embed(chunk))
            metas.append(row_meta)

        try:
            self._collection.upsert(ids=ids, embeddings=embs, documents=docs, metadatas=metas)
            return len(ids)
        except Exception as exc:
            logger.exception("OnyxMemory.memorize failed: %s", exc)
            return 0

    def recall(self, query: str, n_results: int = 5, where: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if not self.available or self._collection is None:
            return []
        q = str(query or "").strip()
        if not q:
            return []
        n = max(1, min(50, int(n_results or 5)))
        try:
            q_emb = self._embed(q)
            payload: dict[str, Any] = {
                "query_embeddings": [q_emb],
                "n_results": n,
                "include": ["documents", "metadatas", "distances"],
            }
            if where:
                payload["where"] = where
            res = self._collection.query(**payload)

            ids = (res.get("ids") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            out: list[dict[str, Any]] = []
            for i in range(min(len(docs), len(metas), len(ids), len(dists))):
                out.append(
                    {
                        "id": str(ids[i]),
                        "text": str(docs[i] or ""),
                        "metadata": dict(metas[i] or {}),
                        "distance": float(dists[i]),
                    }
                )
            return out
        except Exception as exc:
            logger.exception("OnyxMemory.recall failed: %s", exc)
            return []

    def count(self) -> int:
        if not self.available or self._collection is None:
            return 0
        try:
            return int(self._collection.count() or 0)
        except Exception:
            return 0

    def browse(self, limit: int = 200, where: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Browse memories for inspection (sorted by timestamp desc)."""
        if not self.available or self._collection is None:
            return []
        n = max(1, min(2000, int(limit or 200)))
        try:
            payload: dict[str, Any] = {
                "include": ["documents", "metadatas"],
                "limit": n,
            }
            if where:
                payload["where"] = where
            res = self._collection.get(**payload)
            ids = list(res.get("ids") or [])
            docs = list(res.get("documents") or [])
            metas = list(res.get("metadatas") or [])
            rows: list[dict[str, Any]] = []
            for i in range(min(len(ids), len(docs), len(metas))):
                md = dict(metas[i] or {})
                rows.append(
                    {
                        "id": str(ids[i]),
                        "text": str(docs[i] or ""),
                        "metadata": md,
                        "timestamp": str(md.get("timestamp") or md.get("created_at") or ""),
                    }
                )
            rows.sort(key=lambda x: str(x.get("timestamp") or ""), reverse=True)
            return rows[:n]
        except Exception as exc:
            logger.exception("OnyxMemory.browse failed: %s", exc)
            return []

    def delete_ids(self, ids: list[str]) -> int:
        if not self.available or self._collection is None:
            return 0
        clean = [str(x).strip() for x in (ids or []) if str(x).strip()]
        if not clean:
            return 0
        try:
            self._collection.delete(ids=clean)
            return len(clean)
        except Exception as exc:
            logger.exception("OnyxMemory.delete_ids failed: %s", exc)
            return 0
