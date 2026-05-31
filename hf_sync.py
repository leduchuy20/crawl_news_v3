#!/usr/bin/env python3
"""Đồng bộ thư mục data/ với Hugging Face Dataset (có nén gzip).

Thay cho việc commit dataset vào git (repo phình to). Lưu dataset trên HF,
mỗi lần GHA chạy: pull state cũ -> chạy pipeline (NER incremental) -> push lại.

NÉN: pipeline vẫn đọc/ghi `.jsonl` thô ở local. hf_sync nén ở TẦNG SYNC:
  - push: gzip `*.jsonl` -> `*.jsonl.gz` rồi mới upload (giảm ~3.7× dung lượng HF).
  - pull: tải `.gz` về rồi giải nén lại thành `.jsonl` trong data/.
File `.json` nhỏ (checkpoint) giữ nguyên không nén.

Dùng:
    python hf_sync.py pull            # tải + giải nén từ HF về data/
    python hf_sync.py push            # nén + đẩy data/ lên HF
    python hf_sync.py push --all      # đẩy thêm cả file rebuildable (final/cleaned/...)

Env:
    HF_TOKEN     (bắt buộc khi push, và khi pull repo private)
    HF_REPO_ID   (mặc định: huyleduc/crawl-news-vn)
    HF_PRIVATE   (mặc định: 1 -> tạo repo private)
"""
from __future__ import annotations

import argparse
import glob
import gzip
import os
import shutil
import sys
import tempfile

DEFAULT_REPO_ID = "huyleduc/crawl-news-vn"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
GZIP_LEVEL = 6

# File JSONL "nóng" cần giữ trạng thái (NER đắt đỏ). Đuôi `.jsonl*` bắt cả .jsonl lẫn .jsonl.gz.
CORE_JSONL = ["articles_ner*.jsonl*"]
# File JSONL rebuildable (chỉ sync khi --all).
EXTRA_JSONL = [
    "rss_articles*.jsonl*",
    "html_articles*.jsonl*",
    "articles_final*.jsonl*",
    "articles_cleaned*.jsonl*",
    "articles_ready*.jsonl*",
]
# File JSON nhỏ (checkpoint resume/dedup) — KHÔNG nén.
JSON_FILES = ["checkpoint_*.json"]


def _repo_id() -> str:
    return os.environ.get("HF_REPO_ID", DEFAULT_REPO_ID)


def _token():
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")


def _ensure_repo(api, repo_id: str):
    private = os.environ.get("HF_PRIVATE", "1") not in ("0", "false", "False", "")
    api.create_repo(repo_id=repo_id, repo_type="dataset", private=private,
                    exist_ok=True, token=_token())


def _gzip_file(src: str, dst: str):
    """Nén deterministic (mtime=0, KHÔNG nhúng tên file) -> nội dung không đổi thì byte
    y hệt -> HF khỏi re-upload bản trùng."""
    with open(src, "rb") as f_in, open(dst, "wb") as raw_out:
        with gzip.GzipFile(filename="", fileobj=raw_out, mode="wb",
                           compresslevel=GZIP_LEVEL, mtime=0) as f_out:
            shutil.copyfileobj(f_in, f_out, length=1024 * 1024)


def _gunzip_file(src: str, dst: str):
    with gzip.open(src, "rb") as f_in, open(dst, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out, length=1024 * 1024)


def cmd_push(args) -> int:
    from huggingface_hub import HfApi

    repo_id = _repo_id()
    if not _token():
        print("[hf_sync] ERROR: thiếu HF_TOKEN.", file=sys.stderr)
        return 1
    if not os.path.isdir(DATA_DIR):
        print(f"[hf_sync] ERROR: không thấy {DATA_DIR}", file=sys.stderr)
        return 1

    jsonl_patterns = CORE_JSONL + (EXTRA_JSONL if args.all else [])

    # Build staging: gzip jsonl -> .jsonl.gz, copy json as-is.
    staging = tempfile.mkdtemp(prefix="hf_push_")
    n_gz = n_json = 0
    try:
        for pat in jsonl_patterns:
            for src in glob.glob(os.path.join(DATA_DIR, pat)):
                name = os.path.basename(src)
                # Guard: chỉ nhận đúng .jsonl / .jsonl.gz (glob `*.jsonl*` có thể dính
                # file rác như *.jsonl.metadata của HF cache).
                if not (name.endswith(".jsonl") or name.endswith(".jsonl.gz")):
                    continue
                if name.endswith(".gz"):
                    shutil.copy2(src, os.path.join(staging, name))     # đã nén sẵn (legacy)
                else:
                    _gzip_file(src, os.path.join(staging, name + ".gz"))
                n_gz += 1
        for pat in JSON_FILES:
            for src in glob.glob(os.path.join(DATA_DIR, pat)):
                shutil.copy2(src, os.path.join(staging, os.path.basename(src)))
                n_json += 1

        if n_gz == 0 and n_json == 0:
            print("[hf_sync] WARN: không có file khớp để push.", file=sys.stderr)
            return 0

        api = HfApi()
        _ensure_repo(api, repo_id)
        api.upload_folder(
            folder_path=staging,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=args.message,
            token=_token(),
            # Dọn bản .jsonl thô cũ trên HF (nếu lần đầu lỡ push chưa nén) -> chỉ giữ .gz.
            delete_patterns=["*.jsonl"],
        )
        print(f"[hf_sync] pushed {n_gz} jsonl(.gz) + {n_json} json -> {repo_id}")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def cmd_pull(args) -> int:
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import RepositoryNotFoundError

    repo_id = _repo_id()
    os.makedirs(DATA_DIR, exist_ok=True)
    jsonl_patterns = CORE_JSONL + (EXTRA_JSONL if args.all else [])
    allow = jsonl_patterns + JSON_FILES   # `.jsonl*` đã bắt cả .gz

    staging = tempfile.mkdtemp(prefix="hf_pull_")
    try:
        try:
            snapshot_download(repo_id=repo_id, repo_type="dataset", local_dir=staging,
                              allow_patterns=allow, token=_token())
        except RepositoryNotFoundError:
            print(f"[hf_sync] repo {repo_id} chưa tồn tại, bỏ qua pull (first run).")
            return 0

        n = 0
        for root, dirs, files in os.walk(staging):
            # Bỏ qua thư mục cache nội bộ của huggingface_hub (.cache/huggingface/... chứa *.metadata)
            dirs[:] = [dd for dd in dirs if dd != ".cache"]
            for fn in files:
                src = os.path.join(root, fn)
                if fn.endswith(".jsonl.gz"):
                    _gunzip_file(src, os.path.join(DATA_DIR, fn[:-3]))   # bỏ ".gz"
                elif fn.endswith(".jsonl") or fn.endswith(".json"):
                    shutil.copy2(src, os.path.join(DATA_DIR, fn))        # .jsonl thô legacy / .json
                else:
                    continue   # bỏ qua file lạ (vd *.metadata của HF cache)
                n += 1
        print(f"[hf_sync] pulled + giải nén {n} file từ {repo_id} -> {DATA_DIR}")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    p = argparse.ArgumentParser(description="Đồng bộ data/ với HF dataset (gzip)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("pull", help="tải + giải nén data từ HF về")
    pp.add_argument("--all", action="store_true", help="tải cả file rebuildable")
    pp.set_defaults(func=cmd_pull)

    ps = sub.add_parser("push", help="nén + đẩy data/ lên HF")
    ps.add_argument("--all", action="store_true", help="đẩy cả file rebuildable")
    ps.add_argument("-m", "--message", default="chore(data): sync from pipeline")
    ps.set_defaults(func=cmd_push)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
