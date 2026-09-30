"""审计与包线编排。

* 审计：校验 -> 精确 Phase-I 求解 -> 冻结/重放；
* 包线：读取已冻结且**可行**的原审计 -> 校验扰动系数 ->
  Fourier–Motzkin 任意精度有理数消去电流变量 -> 冻结规范化二维半平面；
* 查询：对已冻结包线逐边界代入有理坐标，内点给出一组可复算电流，
  外点稳定标出首条违约边界、严格违反值与代入结果。

原审计与已冻结包线均只读，非法请求不写入、不覆盖任何既有结论。
"""

from __future__ import annotations

from fractions import Fraction

from .envelope import (
    EnvelopeInfeasible,
    ProjectionScaleError,
    evaluate,
    feasible_currents,
    project,
    violation_report,
)
from .envelope_models import parse_envelope_request, parse_point
from .models import AuditPayload, PayloadError, parse_payload
from .simplex import solve
from .storage import (
    AuditStore,
    EnvelopeConflictError,
    IdConflictError,
)

__all__ = [
    "AuditService",
    "IdConflictError",
    "EnvelopeConflictError",
    "PayloadError",
    "ProjectionScaleError",
]


class AuditService:
    def __init__(self, store: AuditStore):
        self.store = store

    # ------------------------- 原审计 -------------------------
    def audit(self, raw_payload) -> tuple[dict, bool]:
        """返回 (冻结记录, 是否为重放命中)。载荷非法时抛 ValueError，不写任何记录。"""
        parsed: AuditPayload = parse_payload(raw_payload)
        result = solve(parsed.variables, parsed.constraints).as_dict()
        result["stable_flags"] = list(parsed.stable_flags)
        record, replayed = self.store.submit(
            parsed.audit_id, parsed.canonical(), result
        )
        return record, replayed

    def fetch(self, audit_id: str) -> dict | None:
        return self.store.get(audit_id)

    # ------------------------- 运行包线 -------------------------
    def create_envelope(self, raw_payload) -> tuple[dict, bool]:
        req = parse_envelope_request(raw_payload)

        source = self.store.get(req.source_audit_id)
        if source is None:
            raise PayloadError(
                f"来源审计不存在: {req.source_audit_id}（请先提交并冻结该审计）"
            )
        if source["result"].get("status") != "feasible":
            raise PayloadError(
                f"来源审计 {req.source_audit_id} 不可行，无法在其上建立运行包线"
            )

        src_payload = source["payload"]
        variables = tuple(src_payload["variables"])
        src_cons = src_payload["constraints"]
        m = len(src_cons)
        if len(req.perturbations) != m:
            raise PayloadError(
                f"扰动系数对数 ({len(req.perturbations)}) 必须等于原约束条数 ({m})"
            )

        A = [tuple(c["coeffs"]) for c in src_cons]
        b = [c["b"] for c in src_cons]
        labels = [c.get("label") for c in src_cons]
        P = [pq[0] for pq in req.perturbations]
        Q = [pq[1] for pq in req.perturbations]

        try:
            env = project(A, b, P, Q)
        except ProjectionScaleError:
            raise
        except EnvelopeInfeasible as exc:  # 防御：来源可行时 (0,0) 必可行
            raise RuntimeError(str(exc)) from exc

        result = env.as_dict(variables, A, b, P, Q, labels)
        result["source_audit_id"] = req.source_audit_id
        result["source_fingerprint"] = source["fingerprint"]
        canonical = req.canonical(source["fingerprint"])
        record, replayed = self.store.submit_envelope(
            req.envelope_id, canonical, result
        )
        return record, replayed

    def fetch_envelope(self, envelope_id: str) -> dict | None:
        return self.store.get_envelope(envelope_id)

    def query_point(self, envelope_id: str, raw_point) -> dict:
        record = self.store.get_envelope(envelope_id)
        if record is None:
            raise LookupError(envelope_id)
        u, v = parse_point(raw_point)
        return self._answer(record, u, v)

    def _answer(self, record: dict, u: Fraction, v: Fraction) -> dict:
        from .envelope import Boundary  # 局部导入避免循环观感

        res = record["result"]
        boundaries = [
            Boundary(
                index=bd["index"],
                alpha=int(bd["alpha"]),
                beta=int(bd["beta"]),
                rhs=int(bd["rhs"]),
                sources=tuple(
                    (int(t["index"]), Fraction(t["multiplier"]))
                    for t in bd["sources"]
                ),
            )
            for bd in res["boundaries"]
        ]
        evals = evaluate(boundaries, u, v)
        first_bad = next((e for e in evals if e.violated), None)
        point = {"u": str(u), "v": str(v)}

        if first_bad is None:
            A = [tuple(int(x) for x in c["coeffs"])
                 for c in res["source_constraints"]]
            b = [int(c["b"]) for c in res["source_constraints"]]
            P = [int(c["p"]) for c in res["source_constraints"]]
            Q = [int(c["q"]) for c in res["source_constraints"]]
            names = res["current_variables"]
            currents, residuals = feasible_currents(
                names, A, b, P, Q, u, v)
            return {
                "envelope_id": record["envelope_id"],
                "point": point,
                "status": "feasible",
                "currents": {name: _frac(x) for name, x in currents.items()},
                "residuals": [
                    {"index": i, "value": _frac(r)}
                    for i, r in enumerate(residuals)
                ],
                "boundary_checks": [e.as_dict() for e in evals],
            }

        return {
            "envelope_id": record["envelope_id"],
            "point": point,
            "status": "infeasible",
            "first_violation": violation_report(first_bad, u, v),
            "boundary_checks": [e.as_dict() for e in evals],
        }


def _frac(x: Fraction) -> str:
    if x.denominator == 1:
        return str(x.numerator)
    return f"{x.numerator}/{x.denominator}"
