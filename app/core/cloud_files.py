from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.core.config import ROOT


@dataclass
class FileInfo:
    name: str
    rel_path: str
    size: int
    modified: dt.datetime


def cloud_bucket() -> str:
    return str(os.getenv("CLOUD_FILES_BUCKET", "")).strip()


def cloud_prefix() -> str:
    return str(os.getenv("CLOUD_FILES_PREFIX", "")).strip().strip("/")


def cloud_enabled() -> bool:
    return bool(cloud_bucket())


def _local_abs(rel_path: str) -> Path:
    rel = str(rel_path or "").strip().lstrip("/")
    return (ROOT / rel).resolve()


def _blob_name(rel_path: str) -> str:
    rel = str(rel_path or "").strip().lstrip("/")
    pref = cloud_prefix()
    return f"{pref}/{rel}" if pref else rel


def _gcs_client():
    try:
        from google.cloud import storage  # type: ignore

        return storage.Client()
    except Exception:
        return None


def exists(rel_path: str) -> bool:
    p = _local_abs(rel_path)
    if p.exists():
        return True
    if not cloud_enabled():
        return False
    client = _gcs_client()
    if client is None:
        return False
    try:
        b = client.bucket(cloud_bucket())
        return bool(b.blob(_blob_name(rel_path)).exists())
    except Exception:
        return False


def read_text(rel_path: str, max_chars: int | None = None) -> str:
    p = _local_abs(rel_path)
    if p.exists() and p.is_file():
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
            return txt[:max_chars] if max_chars and max_chars > 0 else txt
        except Exception:
            pass
    if not cloud_enabled():
        return ""
    client = _gcs_client()
    if client is None:
        return ""
    try:
        b = client.bucket(cloud_bucket())
        blob = b.blob(_blob_name(rel_path))
        if not blob.exists():
            return ""
        txt = blob.download_as_text(encoding="utf-8")
        return txt[:max_chars] if max_chars and max_chars > 0 else txt
    except Exception:
        return ""


def write_text(rel_path: str, content: str) -> bool:
    ok_local = False
    p = _local_abs(rel_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(content or ""), encoding="utf-8")
        ok_local = True
    except Exception:
        ok_local = False
    if not cloud_enabled():
        return ok_local
    client = _gcs_client()
    if client is None:
        return ok_local
    try:
        b = client.bucket(cloud_bucket())
        blob = b.blob(_blob_name(rel_path))
        blob.upload_from_string(str(content or ""), content_type="text/plain; charset=utf-8")
        return True
    except Exception:
        return ok_local


def list_files(rel_dir: str, suffixes: Iterable[str] | None = None) -> list[FileInfo]:
    rel = str(rel_dir or "").strip().strip("/")
    suf = {str(x or "").lower() for x in (suffixes or []) if str(x or "").strip()}
    out: list[FileInfo] = []

    local_dir = _local_abs(rel)
    if local_dir.exists() and local_dir.is_dir():
        for p in local_dir.iterdir():
            if not p.is_file() or p.name.startswith("."):
                continue
            if suf and p.suffix.lower() not in suf:
                continue
            try:
                st = p.stat()
                out.append(
                    FileInfo(
                        name=p.name,
                        rel_path=f"{rel}/{p.name}" if rel else p.name,
                        size=int(st.st_size),
                        modified=dt.datetime.fromtimestamp(st.st_mtime),
                    )
                )
            except Exception:
                continue
        out.sort(key=lambda x: x.modified, reverse=True)
        return out

    if not cloud_enabled():
        return out
    client = _gcs_client()
    if client is None:
        return out
    try:
        b = client.bucket(cloud_bucket())
        prefix = _blob_name(rel + "/")
        for blob in b.list_blobs(prefix=prefix):
            name = str(blob.name or "")
            if not name or name.endswith("/"):
                continue
            leaf = name.split("/")[-1]
            if not leaf or leaf.startswith("."):
                continue
            ext = Path(leaf).suffix.lower()
            if suf and ext not in suf:
                continue
            updated = blob.updated
            if updated is None:
                updated = dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
            out.append(
                FileInfo(
                    name=leaf,
                    rel_path=f"{rel}/{leaf}" if rel else leaf,
                    size=int(blob.size or 0),
                    modified=updated.replace(tzinfo=None) if updated.tzinfo else updated,
                )
            )
    except Exception:
        return []
    out.sort(key=lambda x: x.modified, reverse=True)
    return out
