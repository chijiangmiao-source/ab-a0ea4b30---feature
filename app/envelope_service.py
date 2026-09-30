"""运行包线编排：读取已冻结且可行的审计 -> 精确 FM 投影 -> 冻结/重放 -> 坐标查询。

安全语义：
  * 来源审计不存在、来源本身不可行、投影超出受理规模 → 拒绝（不落任何记录）；
  * envelope_id 复用但来源（指纹）或扰动系数改变 → 冲突，原冻结包线不变；
  * 只读审计存储：创建包线绝不改写原审计；
  * 查询基于冻结记录重建投影，不重新消元、不允许漂移。
"""

from __future__ import annotations

from fractions import Fraction

from .envelope import (
    EnvelopeProjection,
    EnvelopeScaleError,
    Halfplane,
    SourceConstraint,
    first_violation,
    project,
    witness_currents,
)
from .envelope_models import (
    EnvelopePayloadError,
    EnvelopeSpec,
    parse_envelope_header,
    parse_perturbations,
    parse_rational,
)
from .storage import AuditStore, EnvelopeStore


class EnvelopeRejectedError(Exception):
    """业务上拒绝生成包线（来源缺失/不可行/超规模）。code 供 HTTP 层区分。"""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _frac_str(v: Fraction) -> str:
    if v.denominator == 1:
        return str(v.numerator)
    return f"{v.numerator}/{v.denominator}"


def _line_str(alpha: int, beta: int, gamma: int) -> str:
    """规范边界的可读形式 α·p + β·q ≤ γ，跳过零系数与系数 ±1。"""
    parts: list[str] = []

    def term(coef: int, name: str, first: bool) -> str:
        if coef == 0:
            return ""
        sign = "-" if coef < 0 else ("" if first else "+")
        mag = abs(coef)
        body = name if mag == 1 else f"{mag}*{name}"
        return f"{sign}{body}"

    for coef, name in ((alpha, "p"), (beta, "q")):
        t = term(coef, name, not parts)
        if t:
            parts.append(t)
    lhs = "".join(parts) if parts else "0"
    return f"{lhs} <= {gamma}"


def _derivation(
    sources: tuple[SourceConstraint, ...], h: Halfplane
) -> dict:
    """把一条半平面的来源合并式逐项展开，供页面与外部独立复算。"""
    terms = []
    cur = [Fraction(0) for _ in sources[0].coeffs]
    sp = sq = sr = Fraction(0)
    for i, (mu, s) in enumerate(zip(h.multipliers, sources)):
        if mu == 0:
            continue
        weighted = tuple(mu * a for a in s.coeffs)
        wp, wq, wr = -mu * s.u, -mu * s.v, mu * s.b
        for j, w in enumerate(weighted):
            cur[j] += w
        sp += wp
        sq += wq
        sr += wr
        terms.append({
            "index": i,
            "label": s.label,
            "multiplier": _frac_str(mu),
            "weighted_coeffs": [_frac_str(w) for w in weighted],
            "weighted_p": _frac_str(wp),
            "weighted_q": _frac_str(wq),
            "weighted_rhs": _frac_str(wr),
        })
    return {
        "terms": terms,
        "combined_coeffs": [_frac_str(c) for c in cur],  # 必须全为 0
        "combined_p": _frac_str(sp),
        "combined_q": _frac_str(sq),
        "combined_rhs": _frac_str(sr),
        "normalized_line": _line_str(h.alpha, h.beta, h.gamma),
    }


def _serialize_projection(proj: EnvelopeProjection) -> dict:
    sources = [
        {
            "index": i,
            "coeffs": list(s.coeffs),
            "b": s.b,
            "u": s.u,
            "v": s.v,
            "label": s.label,
        }
        for i, s in enumerate(proj.sources)
    ]
    halfplanes = []
    for h in proj.halfplanes:
        halfplanes.append({
            "index": h.index,
            "alpha": h.alpha,
            "beta": h.beta,
            "gamma": h.gamma,
            "line": _line_str(h.alpha, h.beta, h.gamma),
            "multipliers": [_frac_str(mu) for mu in h.multipliers],
            "derivation": _derivation(proj.sources, h),
        })
    return {
        "variables": list(proj.variables),
        "sources": sources,
        "projection": {
            "method": "exact-rational/fourier-motzkin",
            "kind": "whole_plane" if proj.whole_plane else "halfplanes",
            "whole_plane": proj.whole_plane,
            "halfplanes": halfplanes,
            "boundary_count": len(halfplanes),
            "combinations_per_stage": proj.combinations_per_stage,
            "elimination_order": list(proj.elimination_order),
        },
    }


def _projection_from_record(record: dict) -> EnvelopeProjection:
    """从冻结记录重建投影对象（只重建数据，不重新消元）。"""
    sources = tuple(
        SourceConstraint(
            coeffs=tuple(s["coeffs"]),
            b=s["b"],
            u=s["u"],
            v=s["v"],
            label=s.get("label"),
        )
        for s in record["sources"]
    )
    halfplanes = [
        Halfplane(
            index=h["index"],
            alpha=h["alpha"],
            beta=h["beta"],
            gamma=h["gamma"],
            multipliers=tuple(Fraction(m) for m in h["multipliers"]),
        )
        for h in record["projection"]["halfplanes"]
    ]
    halfplanes.sort(key=lambda x: x.index)
    return EnvelopeProjection(
        variables=tuple(record["variables"]),
        sources=sources,
        halfplanes=halfplanes,
    )


class EnvelopeService:
    def __init__(self, audit_store: AuditStore, envelope_store: EnvelopeStore):
        self.audit_store = audit_store
        self.envelope_store = envelope_store

    def create(self, raw_payload) -> tuple[dict, bool]:
        # 先把头（两个编号）校验出来：编号格式非法 → 400；
        # 格式合法但来源不存在 → 404。
        envelope_id, source_id = parse_envelope_header(raw_payload)
        source = self.audit_store.get(source_id)
        if source is None:
            raise EnvelopeRejectedError(
                "source_not_found",
                f"来源审计不存在或尚未冻结: {source_id!r}",
            )
        if source["result"]["status"] != "feasible":
            raise EnvelopeRejectedError(
                "source_infeasible",
                "来源审计不可行；无运行点可扰动，拒绝建立包线",
            )

        n_constraints = len(source["payload"]["constraints"])
        us, vs = parse_perturbations(raw_payload, n_constraints)
        spec = EnvelopeSpec(
            envelope_id=envelope_id,
            source_audit_id=source_id,
            us=us,
            vs=vs,
        )

        sources = tuple(
            SourceConstraint(
                coeffs=tuple(int(a) for a in con["coeffs"]),
                b=con["b"],
                u=u,
                v=v,
                label=con.get("label"),
            )
            for con, u, v in zip(
                source["payload"]["constraints"], spec.us, spec.vs
            )
        )
        try:
            proj = project(source["payload"]["variables"], sources)
        except EnvelopeScaleError as exc:
            raise EnvelopeRejectedError(
                "envelope_too_large", str(exc)
            ) from exc

        body = _serialize_projection(proj)
        body["source_audit_id"] = source["audit_id"]
        body["source_fingerprint"] = source["fingerprint"]
        canonical = spec.canonical(source["fingerprint"])
        record, replayed = self.envelope_store.submit(
            spec.envelope_id, canonical, body
        )
        return record, replayed

    def fetch(self, envelope_id: str) -> dict | None:
        return self.envelope_store.get(envelope_id)

    def query(self, envelope_id: str, raw) -> dict:
        record = self.envelope_store.get(envelope_id)
        if record is None:
            raise EnvelopeRejectedError(
                "envelope_not_found", f"包线不存在或尚未冻结: {envelope_id}"
            )
        if not isinstance(raw, dict):
            raise EnvelopePayloadError("请求体必须是 JSON 对象")
        p = parse_rational(raw.get("p"), "p（功率漂移）")
        q = parse_rational(raw.get("q"), "q（场强偏置）")

        proj = _projection_from_record(record)
        viol = first_violation(proj, p, q)
        base = {
            "envelope_id": envelope_id,
            "p": _frac_str(p),
            "q": _frac_str(q),
        }
        if viol is not None:
            lhs = viol.alpha * p + viol.beta * q
            base.update({
                "feasible": False,
                "violation": {
                    "index": viol.index,
                    "line": _line_str(viol.alpha, viol.beta, viol.gamma),
                    "alpha": viol.alpha,
                    "beta": viol.beta,
                    "gamma": viol.gamma,
                    "substitution": _frac_str(lhs),       # 代入结果
                    "strict_excess": _frac_str(lhs - viol.gamma),  # 严格违反值 > 0
                    "derivation": _derivation(proj.sources, viol),
                },
            })
            return base

        currents, margins = witness_currents(proj, p, q)
        base.update({
            "feasible": True,
            "currents": {name: _frac_str(val) for name, val in currents.items()},
            "margins": [
                {
                    "index": i,
                    "label": s.label,
                    "rhs": _frac_str(
                        Fraction(s.b) + s.u * p + s.v * q
                    ),
                    "residual": _frac_str(margin),
                }
                for i, (s, margin) in enumerate(zip(proj.sources, margins))
            ],
        })
        return base
