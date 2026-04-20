#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
partition_io.py
---------------
Helper chung cho 01/02/03: đọc nhiều input partition (glob), ghi output có rotation
theo ngưỡng 89MB (an toàn dưới 100MB GitHub, tính cả overhead binary mode).

Naming pattern: {base}.jsonl  (current)
              + {base}_{YYYY-MM}.jsonl        (rotated, nếu trùng tháng thì _2, _3, ...)
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set

ROTATE_THRESHOLD_BYTES = 89 * 1024 * 1024  # 89MB — chừa buffer dưới 100MB


def expand_inputs(patterns: List[str]) -> List[str]:
    """Expand list of globs/paths → sorted unique file list. Bỏ file không tồn tại."""
    seen: Set[str] = set()
    out: List[str] = []
    for p in patterns:
        matched = sorted(glob.glob(p))
        if not matched and os.path.exists(p):
            matched = [p]
        for m in matched:
            if m not in seen:
                seen.add(m)
                out.append(m)
    return out


def iter_records(input_paths: List[str]) -> Iterator[Dict[str, Any]]:
    """Yield từng record JSON từ danh sách file input. Bỏ qua JSON lỗi."""
    for path in input_paths:
        with open(path, "rb") as fp:
            for raw in fp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def partition_paths(base_path: str) -> List[str]:
    """Trả về các partition đã rotate (không bao gồm file current)."""
    base, ext = os.path.splitext(base_path)
    return sorted(glob.glob(f"{base}_*{ext}"))


def all_partition_paths(base_path: str) -> List[str]:
    """Partition đã rotate + file current (nếu tồn tại)."""
    out = partition_paths(base_path)
    if os.path.exists(base_path):
        out.append(base_path)
    return out


def load_existing_ids(base_path: str) -> Set[str]:
    """Load tất cả `id` từ output hiện có (cho incremental mode)."""
    ids: Set[str] = set()
    for p in all_partition_paths(base_path):
        with open(p, "rb") as fp:
            for raw in fp:
                try:
                    o = json.loads(raw.decode("utf-8", errors="replace"))
                    if "id" in o:
                        ids.add(o["id"])
                except json.JSONDecodeError:
                    continue
    return ids


def clear_all_partitions(base_path: str) -> int:
    """Xoá current + mọi partition. Dùng cho full-rebuild mode (cleanup)."""
    paths = all_partition_paths(base_path)
    for p in paths:
        os.remove(p)
    return len(paths)


class PartitionedJsonlWriter:
    """
    Append JSON records vào base_path; khi > ROTATE_THRESHOLD_BYTES, rename thành
    {base}_{YYYY-MM}[_N].jsonl rồi tạo file mới. Ghi binary mode (avoid CRLF).

    Usage:
        w = PartitionedJsonlWriter("data/cleaned.jsonl")
        with w:
            w.write(record_dict)
            ...
    """

    def __init__(self, base_path: str, rotate_bytes: int = ROTATE_THRESHOLD_BYTES):
        self.base_path = base_path
        self.rotate_bytes = rotate_bytes
        os.makedirs(os.path.dirname(base_path) or ".", exist_ok=True)
        self._fp = None
        self._bytes = 0
        self._count = 0
        self._total_count = 0

    def __enter__(self):
        self._open_current()
        return self

    def __exit__(self, *args):
        self.close()

    def _open_current(self):
        if self._fp is not None:
            return
        self._fp = open(self.base_path, "ab")
        self._bytes = os.path.getsize(self.base_path) if os.path.exists(self.base_path) else 0

    def _next_rotated_name(self) -> str:
        base, ext = os.path.splitext(self.base_path)
        month = datetime.now().strftime("%Y-%m")
        cand = f"{base}_{month}{ext}"
        n = 2
        while os.path.exists(cand):
            cand = f"{base}_{month}_{n}{ext}"
            n += 1
        return cand

    def _rotate(self):
        if self._fp is not None:
            self._fp.close()
            self._fp = None
        if os.path.exists(self.base_path) and os.path.getsize(self.base_path) > 0:
            target = self._next_rotated_name()
            os.rename(self.base_path, target)
        self._bytes = 0
        self._open_current()

    def write(self, record: Dict[str, Any]):
        if self._fp is None:
            self._open_current()
        payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        if self._bytes + len(payload) > self.rotate_bytes and self._bytes > 0:
            self._rotate()
        self._fp.write(payload)
        self._bytes += len(payload)
        self._count += 1
        self._total_count += 1

    def flush(self):
        if self._fp is not None:
            self._fp.flush()

    def close(self):
        if self._fp is not None:
            self._fp.close()
            self._fp = None
