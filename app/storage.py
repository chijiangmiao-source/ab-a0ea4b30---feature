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
    def __init__(self, existing: dict, new_fingerprint: str):
        self.existing = existing
        self.new_fingerprint = new_fingerprint
        super().__init__("envelope_id 已绑定不同来源或扰动系数")


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

    def _envelope_path(self, envelope_id: str) -> str:
        safe = hashlib.sha256(("\x00env:" + envelope_id).encode("utf-8")).hexdigest()
        return os.path.join(self.data_dir, f"env-{safe}.json")

    def get_envelope(self, envelope_id: str) -> dict | None:
        path = self._envelope_path(envelope_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def submit_envelope(
        self, envelope_id: str, payload_canonical: dict, result: dict
    ) -> tuple[dict, bool]:
        """冻结包线记录，语义与审计相同：同号同载荷重放，改来源/系数即冲突。

        指纹中的规范载荷含来源审计指纹，故“复用派生标识却改变来源或系数”
        必然得到不同指纹而被拒绝。
        """
        new_fp = fingerprint(payload_canonical)
        with self._lock:
            existing = self.get_envelope(envelope_id)
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
                "payload": payload_canonical,
                "result": result,
                "method": "fourier-motzkin/exact-rational",
            }
            self._write_atomic_envelope(envelope_id, record)
            return record, False

    def _write_atomic_envelope(self, envelope_id: str, record: dict) -> None:
        path = self._envelope_path(envelope_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

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
