"""审计冻结记录存储。

同一 audit_id + 完全相同载荷 -> 返回同一记录（幂等）；
同一 audit_id + 不同载荷     -> 冲突，原记录保持不变；
非法载荷在写入前即被拒绝，不会残留或覆盖任何结论。

每条记录一个 JSON 文件，临时文件 + fsync + 原子 rename 落盘，
进程内以锁串行化“检查-写入”，避免并发首次提交产生双结论。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone


class IdConflictError(Exception):
    def __init__(self, existing: dict, new_fingerprint: str):
        self.existing = existing
        self.new_fingerprint = new_fingerprint
        super().__init__("audit_id 已绑定不同载荷")


class EnvelopeConflictError(Exception):
    """envelope_id 已绑定不同的来源或扰动系数；原冻结包线保持不变。"""

    def __init__(self, existing: dict, new_fingerprint: str):
        self.existing = existing
        self.new_fingerprint = new_fingerprint
        super().__init__("envelope_id 已绑定不同来源或系数")


def canonical_json(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def fingerprint(payload: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


class AuditStore:
    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, audit_id: str) -> str:
        safe = hashlib.sha256(audit_id.encode("utf-8")).hexdigest()
        return os.path.join(self.data_dir, f"{safe}.json")

    def get(self, audit_id: str) -> dict | None:
        path = self._path(audit_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def submit(self, audit_id: str, payload_canonical: dict, result: dict) -> tuple[dict, bool]:
        """返回 (记录, 是否为重放)。冲突时抛 :class:`IdConflictError`。"""
        new_fp = fingerprint(payload_canonical)
        with self._lock:
            existing = self.get(audit_id)
            if existing is not None:
                if existing["fingerprint"] != new_fp:
                    raise IdConflictError(existing, new_fp)
                return existing, True

            record = {
                "audit_id": audit_id,
                "created_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "fingerprint": new_fp,
                "payload": payload_canonical,
                "result": result,
                "method": "phase-I-simplex/exact-rational/Bland",
            }
            self._write_atomic(audit_id, record)
            return record, False

    def _write_atomic(self, audit_id: str, record: dict) -> None:
        path = self._path(audit_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


class EnvelopeStore:
    """运行包线冻结存储（与审计记录分开的 envelopes/ 子目录）。

    与 :class:`AuditStore` 同样的语义：同编号 + 同指纹重放命中；
    同编号 + 不同来源/系数 → :class:`EnvelopeConflictError`，原冻结不变。
    写审计记录与写包线记录互不影响，包线被拒绝绝不改写原审计。
    """

    SUBDIR = "envelopes"

    def __init__(self, data_dir: str):
        self.dir = os.path.join(os.path.abspath(data_dir), self.SUBDIR)
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, envelope_id: str) -> str:
        safe = hashlib.sha256(envelope_id.encode("utf-8")).hexdigest()
        return os.path.join(self.dir, f"{safe}.json")

    def get(self, envelope_id: str) -> dict | None:
        try:
            with open(self._path(envelope_id), "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def submit(
        self, envelope_id: str, spec_canonical: dict, record_body: dict
    ) -> tuple[dict, bool]:
        """返回 (记录, 是否重放)。冲突时抛 :class:`EnvelopeConflictError`。"""
        new_fp = fingerprint(spec_canonical)
        with self._lock:
            existing = self.get(envelope_id)
            if existing is not None:
                if existing["fingerprint"] != new_fp:
                    raise EnvelopeConflictError(existing, new_fp)
                return existing, True
            record = {
                "envelope_id": envelope_id,
                "created_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "fingerprint": new_fp,
                "spec": spec_canonical,
                **record_body,
            }
            path = self._path(envelope_id)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return record, False
