from __future__ import annotations

import argparse
import mimetypes
from pathlib import Path

from google.cloud import storage


def _content_type(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or "application/octet-stream"


def _upload_tree(client: storage.Client, bucket_name: str, src_root: Path, dst_prefix: str) -> int:
    bucket = client.bucket(bucket_name)
    uploaded = 0
    if not src_root.exists() or not src_root.is_dir():
        return 0
    for p in src_root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src_root).as_posix()
        blob_name = f"{dst_prefix.strip('/')}/{rel}".strip("/")
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(str(p), content_type=_content_type(p))
        uploaded += 1
    return uploaded


def _upload_file(client: storage.Client, bucket_name: str, src_file: Path, dst_path: str) -> int:
    if not src_file.exists() or not src_file.is_file():
        return 0
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(dst_path.strip("/"))
    blob.upload_from_filename(str(src_file), content_type=_content_type(src_file))
    return 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync local report/data files to GCS for cloud runtime parity.")
    ap.add_argument("--bucket", required=True, help="GCS bucket name")
    ap.add_argument("--prefix", default="", help="Optional GCS prefix (example: investor)")
    ap.add_argument("--root", default=".", help="Project root path")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    pref = str(args.prefix or "").strip().strip("/")

    def dst(path: str) -> str:
        return f"{pref}/{path}".strip("/") if pref else path.strip("/")

    client = storage.Client()
    uploaded = 0
    uploaded += _upload_tree(client, args.bucket, root / "reports", dst("reports"))
    uploaded += _upload_tree(client, args.bucket, root / "outputs", dst("outputs"))
    uploaded += _upload_tree(client, args.bucket, root / "filings", dst("filings"))
    uploaded += _upload_tree(client, args.bucket, root / "filing_docs", dst("filing_docs"))
    uploaded += _upload_file(client, args.bucket, root / "data" / "portfolio.csv", dst("data/portfolio.csv"))
    uploaded += _upload_file(client, args.bucket, root / "data" / "my_watchlist.txt", dst("data/my_watchlist.txt"))
    uploaded += _upload_file(client, args.bucket, root / "data" / "cash_balances.csv", dst("data/cash_balances.csv"))
    uploaded += _upload_file(client, args.bucket, root / "data" / "sec_sync_state.json", dst("data/sec_sync_state.json"))
    uploaded += _upload_file(client, args.bucket, root / "data" / "reports_read_state.json", dst("data/reports_read_state.json"))

    print({"uploaded_objects": uploaded, "bucket": args.bucket, "prefix": pref})


if __name__ == "__main__":
    main()
