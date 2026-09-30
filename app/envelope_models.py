"""运行包线请求的整数级校验与有理坐标解析。

* 创建包线时，两个扰动系数必须是 **JSON 整数**（与审计同一纪律：
  拒绝浮点/布尔/字符串数字），且每条原约束恰好一对；
* 坐标查询接受精确有理数：JSON 整数或形如 "p/q"、"1.5" 的**文本**
  （经 :class:`fractions.Fraction` 精确解析，绝不走 JS/JSON 浮点），
  分母为零、NaN、指数记法等一律拒绝，并限制分子分母位宽。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

from .models import (
    MAX_ID_LEN,
    MAX_INT_BITS,
    _ID_RE,
    _is_plain_int,
)

MAX_PERTURBATIONS = 48  # 与审计最多 48 条原约束一致
_COORD_FRACTION_RE = re.compile(r"^[+-]?\d+(?:\s*/\s*[+-]?\d+)?$")
_COORD_DECIMAL_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+)$")


class EnvelopePayloadError(ValueError):
    """包线载荷非法；不计算、不冻结、不覆盖。"""


@dataclass(frozen=True)
class EnvelopeSpec:
    envelope_id: str
    source_audit_id: str
    us: tuple[int, ...]
    vs: tuple[int, ...]

    def canonical(self, source_fingerprint: str) -> dict:
        """幂等比较用规范结构。

        绑定**来源指纹**：复用派生标识却改变来源（或来源内容随指纹变化）
        都会得到不同指纹而被拒绝；扰动系数变化同理。
        """
        return {
            "envelope_id": self.envelope_id,
            "source_audit_id": self.source_audit_id,
            "source_fingerprint": source_fingerprint,
            "perturbations": [
                {"u": u, "v": v} for u, v in zip(self.us, self.vs)
            ],
        }


def _bounded_int(v, what: str) -> int:
    if not _is_plain_int(v):
        raise EnvelopePayloadError(f"{what} 必须是整数")
    if abs(v).bit_length() > MAX_INT_BITS:
        raise EnvelopePayloadError(f"{what} 超出允许的整数范围")
    return v


def _validate_id(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EnvelopePayloadError(f"{field} 必须是非空字符串")
    value = value.strip()
    if len(value) > MAX_ID_LEN or not _ID_RE.match(value):
        raise EnvelopePayloadError(
            f"{field} 仅允许字母、数字、点、下划线、连字符与 @，且不超过 "
            f"{MAX_ID_LEN} 个字符"
        )
    return value


def parse_envelope_header(obj) -> tuple[str, str]:
    """只校验并取出 (envelope_id, source_audit_id)。

    供服务层在读取来源审计前使用，使“来源不存在 (404)”与
    “载荷格式非法 (400)”能被正确区分。
    """
    if not isinstance(obj, dict):
        raise EnvelopePayloadError("请求体必须是 JSON 对象")
    envelope_id = _validate_id(obj.get("envelope_id"), "envelope_id")
    source_id = _validate_id(obj.get("source_audit_id"), "source_audit_id")
    return envelope_id, source_id


def parse_perturbations(obj, expected_constraints: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """校验扰动系数对：必须恰好每条原约束一对，均为 JSON 整数。"""
    raw = obj.get("perturbations")
    if not isinstance(raw, list) or not (1 <= len(raw) <= MAX_PERTURBATIONS):
        raise EnvelopePayloadError(
            f"perturbations 必须是 1 到 {MAX_PERTURBATIONS} 对扰动系数的数组"
        )
    if len(raw) != expected_constraints:
        raise EnvelopePayloadError(
            f"扰动系数对数 ({len(raw)}) 必须与来源审计原约束条数 "
            f"({expected_constraints}) 严格相等，不得增删"
        )

    us: list[int] = []
    vs: list[int] = []
    for k, item in enumerate(raw):
        if not isinstance(item, dict):
            raise EnvelopePayloadError(f"第 {k + 1} 对扰动系数必须是对象")
        u = _bounded_int(item.get("u"), f"第 {k + 1} 对功率漂移系数 u")
        v = _bounded_int(item.get("v"), f"第 {k + 1} 对场强偏置系数 v")
        us.append(u)
        vs.append(v)
    return tuple(us), tuple(vs)


def parse_envelope_payload(obj, expected_constraints: int) -> EnvelopeSpec:
    """解析并校验创建包线的载荷（头部 + 扰动对一次完成）。"""
    envelope_id, source_id = parse_envelope_header(obj)
    us, vs = parse_perturbations(obj, expected_constraints)
    return EnvelopeSpec(
        envelope_id=envelope_id,
        source_audit_id=source_id,
        us=us,
        vs=vs,
    )


def parse_rational(value, what: str) -> Fraction:
    """解析查询坐标：仅接受 JSON 整数或精确有理文本，拒绝一切浮点。"""
    if _is_plain_int(value):
        return Fraction(value)
    if not isinstance(value, str):
        raise EnvelopePayloadError(
            f"{what} 必须是整数或有理分数字符串（如 3、-1/2、1.25）"
        )
    text = value.strip()
    if not text or len(text) > 2 * MAX_INT_BITS // 8:
        raise EnvelopePayloadError(f"{what} 缺失或过长")
    if not (_COORD_FRACTION_RE.match(text)
            or _COORD_DECIMAL_RE.match(text)):
        raise EnvelopePayloadError(
            f"{what} 不是合法有理数（仅接受整数、p/q 或有限小数文本）: {value!r}"
        )
    try:
        frac = Fraction(text.replace(" ", ""))
    except (ZeroDivisionError, ValueError):
        raise EnvelopePayloadError(f"{what} 分母不能为零")
    if (frac.numerator.bit_length() > MAX_INT_BITS
            or frac.denominator.bit_length() > MAX_INT_BITS):
        raise EnvelopePayloadError(f"{what} 超出允许的位宽")
    return frac
