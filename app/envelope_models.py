"""包线创建与坐标查询载荷的整数/有理数级校验。

* 扰动系数必须是 **JSON 整数**（拒绝浮点、布尔、字符串数字、超大整数），
  且每条原约束恰好填写两个（功率漂移 p、场强偏置 q）；
* 查询坐标必须是精确有理文本 ``"p/q"`` 或整数（也接受 JSON 整数），
  分母为零、浮点、布尔一律拒绝，从源头杜绝近似进入判定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

from .models import MAX_ID_LEN, MAX_INT_BITS, PayloadError, _ID_RE, _is_plain_int

_RAT_RE = re.compile(r"^[+-]?\d+(?:/[+-]?\d+)?$")
MAX_PERTURBATIONS = 48  # 与原约束上限一致


@dataclass(frozen=True)
class EnvelopeRequest:
    envelope_id: str
    source_audit_id: str
    perturbations: tuple[tuple[int, int], ...]

    def canonical(self, source_fingerprint: str) -> dict:
        return {
            "envelope_id": self.envelope_id,
            "source_audit_id": self.source_audit_id,
            "source_fingerprint": source_fingerprint,
            "perturbations": [list(pq) for pq in self.perturbations],
        }


def _id_field(obj: dict, key: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise PayloadError(f"{key} 必须是非空字符串")
    v = v.strip()
    if len(v) > MAX_ID_LEN or not _ID_RE.match(v):
        raise PayloadError(
            f"{key} 仅允许字母、数字、点、下划线、连字符与 @，且不超过 "
            f"{MAX_ID_LEN} 个字符"
        )
    return v


def _bounded_int(v, what: str) -> int:
    if not _is_plain_int(v):
        raise PayloadError(f"{what} 必须是整数")
    if abs(v).bit_length() > MAX_INT_BITS:
        raise PayloadError(f"{what} 超出允许的整数范围")
    return v


def parse_envelope_request(obj) -> EnvelopeRequest:
    if not isinstance(obj, dict):
        raise PayloadError("请求体必须是 JSON 对象")
    envelope_id = _id_field(obj, "envelope_id")
    source_audit_id = _id_field(obj, "source_audit_id")

    raw = obj.get("perturbations")
    if not isinstance(raw, list) or not (1 <= len(raw) <= MAX_PERTURBATIONS):
        raise PayloadError(
            f"perturbations 必须是 1 到 {MAX_PERTURBATIONS} 对扰动系数的数组"
        )
    perts: list[tuple[int, int]] = []
    for k, pair in enumerate(raw):
        if not isinstance(pair, list) or len(pair) != 2:
            raise PayloadError(
                f"第 {k + 1} 对扰动必须是含两个整数 (p, q) 的数组"
            )
        p = _bounded_int(pair[0], f"第 {k + 1} 条原约束的功率漂移系数 p")
        q = _bounded_int(pair[1], f"第 {k + 1} 条原约束的场强偏置系数 q")
        perts.append((p, q))

    return EnvelopeRequest(
        envelope_id=envelope_id,
        source_audit_id=source_audit_id,
        perturbations=tuple(perts),
    )


def parse_rational(v, what: str) -> Fraction:
    if _is_plain_int(v):
        return Fraction(v)
    if not isinstance(v, str):
        raise PayloadError(f"{what} 必须是有理文本（如 \"1/2\"）或整数")
    s = v.strip()
    if not _RAT_RE.match(s):
        raise PayloadError(f"{what} 不是合法有理数: {s}")
    parts = s.split("/")
    try:
        if len(parts) == 1:
            return Fraction(int(parts[0]))
        den = int(parts[1])
        if den == 0:
            raise PayloadError(f"{what} 分母不能为零: {s}")
        return Fraction(int(parts[0]), den)
    except ValueError:
        raise PayloadError(f"{what} 不是合法有理数: {s}") from None


def parse_point(obj) -> tuple[Fraction, Fraction]:
    if not isinstance(obj, dict):
        raise PayloadError("请求体必须是 JSON 对象")
    if "u" not in obj or "v" not in obj:
        raise PayloadError("坐标查询必须同时给出 u 与 v")
    return parse_rational(obj["u"], "功率漂移坐标 u"), parse_rational(obj["v"], "场强偏置坐标 v")
