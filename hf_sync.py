#!/usr/bin/env python3
"""Đồng bộ thư mục data/ với Hugging Face Dataset.

Thay cho việc commit dataset vào git (repo phình to). Lưu dataset trên HF,
mỗi lần GHA chạy: pull state cũ -> chạy pipeline (NER incremental) -> push lại.

Dùng:
    python hf_sync.py pull            # tải data từ HF về data/
    python hf_sync.py push            # đẩy data/ lên HF
    python hf_sync.py push --all      # đẩy thêm cả file rebuildable (final/cleaned/...)

Env:
    HF_TOKEN     (bắt buộc khi push, và khi pull repo private)
    HF_REPO_ID   (mặc định: huyleduc/crawl-news-vn)
    HF_PRIVATE   (mặc định: 1 -> tạo repo private)
"""
from __future__ import annotations

import argparse
import os
import sys

DEFAULT_REPO_ID = "huyleduc/crawl-news-vn"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# File "nóng" cần giữ trạng thái giữa các lần chạy:
#  - articles_ner*  : output NER đắt đỏ (NER incremental skip ID đã có)
#  - checkpoint_*   : resume + dedup của crawler
# Đuôi *.jsonl* bắt cả .jsonl (legacy) lẫn .jsonl.gz (mới).
CORE_PATTERNS = [
    "articles_ner*.jsonl*",
    "checkpoint_*.json",
]

# File rebuildable từ pipeline (chỉ push khi --all, để backup đầy đủ).
EXTRA_PATTERNS = [
    "rss_articles*.jsonl*",
    "html_articles*.jsonl*",
    "articles_final*.jsonl*",
    "articles_cleaned*.jsonl*",
    "articles_ready*.jsonl*",
]

# Không bao giờ đẩy lên HF.
IGNORE_PATTERNS = ["*.log", "*.rar", "*.zip", "*.7z", "*.tar.gz"]


def _repo_id() -> str:
    return os.environ.get("HF_REPO_ID", DEFAULT_REPO_ID)


def _token() -> str | None:
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")


def _ensure_repo(api, repo_id: str):
    private = os.environ.get("HF_PRIVATE", "1") not in ("0", "false", "False", "")
    api.create_repo(
        repo_id=repo_id,
        repo_type="dataset",
        private=private,
        exist_ok=True,
        token=_token(),
    )


def cmd_pull(args) -> int:
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import RepositoryNotFoundError

    repo_id = _repo_id()
    os.makedirs(DATA_DIR, exist_ok=True)
    patterns = CORE_PATTERNS + (EXTRA_PATTERNS if args.all else [])
    try:
        snapshot_download(
            repo_id=repo_id,
            repo_type="dataset",
            local_dir=DATA_DIR,
            allow_patterns=patterns,
            token=_token(),
        )
        print(f"[hf_sync] pulled {patterns} from {repo_id} -> {DATA_DIR}")
    except RepositoryNotFoundError:
        # Lần chạy đầu tiên: repo chưa tồn tại -> không sao, pipeline sẽ tạo mới.
        print(f"[hf_sync] repo {repo_id} chưa tồn tại, bỏ qua pull (first run).")
    return 0


def cmd_push(args) -> int:
    from huggingface_hub import HfApi

    repo_id = _repo_id()
    if not _token():
        print("[hf_sync] ERROR: thiếu HF_TOKEN.", file=sys.stderr)
        return 1
    if not os.path.isdir(DATA_DIR):
        print(f"[hf_sync] ERROR: không thấy {DATA_DIR}", file=sys.stderr)
        return 1

    api = HfApi()
    _ensure_repo(api, repo_id)
    patterns = CORE_PATTERNS + (EXTRA_PATTERNS if args.all else [])
    api.upload_folder(
        folder_path=DATA_DIR,
        repo_id=repo_id,
        repo_type="dataset",
        allow_patterns=patterns,
        ignore_patterns=IGNORE_PATTERNS,
        commit_message=args.message,
        token=_token(),
    )
    print(f"[hf_sync] pushed {patterns} -> {repo_id}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Đồng bộ data/ với HF dataset")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("pull", help="tải data từ HF về")
    pp.add_argument("--all", action="store_true", help="tải cả file rebuildable")
    pp.set_defaults(func=cmd_pull)

    ps = sub.add_parser("push", help="đẩy data/ lên HF")
    ps.add_argument("--all", action="store_true", help="đẩy cả file rebuildable")
    ps.add_argument("-m", "--message", default="chore(data): sync from pipeline")
    ps.set_defaults(func=cmd_push)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
