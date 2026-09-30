"""精确有理二维运行包线：Fourier–Motzkin 消去电流变量并完整投影扰动平面。

给定一条**已可行**的原审计

    a_i · x ≤ b_i                (i = 1..m, 系数/右端均为整数)

工程师为每条原约束填写两个整数扰动系数 (p_i, q_i)（功率漂移 u、场强偏置 v），
共同变化后的系统为

    a_i · x + p_i·u + q_i·v ≤ b_i

二维运行包线即该系统在 (u, v) 平面上的精确投影：

    E = { (u,v) ∈ ℚ² | ∃ x ∈ ℚⁿ 使全部约束成立 }

算法（全程 :class:`fractions.Fraction`，无抽样、无浮点）：

  * **Fourier–Motzkin 消元**逐个消去电流变量 x_1..x_n：
    按待消变量系数为正/为负/为零分组，正行与负行分别乘正整数倍
    ``(-c_n)`` 与 ``c_p`` 后相加（系数为零的行原样保留）。
    组合系数恒正 ⇒ 每条导出不等式都是原约束的非负有理合并；
  * 每行立即做**正向整数规范化**（乘公分母、除以各系数与右端的最大公约数，
    **绝不乘 -1**——≤ 不等式乘负号会反向），并按键去重，
    因此比例重复边界只保留一条；同方向平行边界取最紧右端；
  * 每条结果边界保留**来源合并式** λ_i（非负有理乘子），
    代码运行时断言 Σλ_i·a_i=0、Σλ_i·p_i=α、Σλ_i·q_i=β、Σλ_i·b_i=B；
  * 全零系数行 0 ≤ B：B<0 表示包线为空（对可行来源只会是防御性分支），
    B≥0 为重言式，直接丢弃；
  * 零扰动 ⇒ 边界集合为空，表示整个 (u,v) 平面，``bounded=False``，
    绝不会被错误封闭；有界性用 recession cone
    ``{d: αd_u+βd_v ≤ 0}`` 是否含非零方向精确判定，并给出无界见证方向。

中间约束数超过 :data:`MAX_FM_ROWS` 即抛 :class:`ProjectionScaleError`，
由上层以“超过受理规模”拒绝，绝不退化为近似。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import gcd
from typing import Sequence

from .simplex import Constraint, solve

# 受理规模：与审计一致（1–8 个电流变量、1–48 条原约束）；
# FM 中间膨胀设硬上限，超限拒绝创建而非耗时求解或抽样近似。
MAX_FM_ROWS = 1024


class ProjectionScaleError(ValueError):
    """投影中间约束数超过受理规模；创建必须被拒绝。"""


class EnvelopeInfeasible(RuntimeError):
    """理论上不应发生：来源审计可行时 (0,0) 必在包线内。"""


@dataclass(frozen=True)
class _Row:
    """FM 中间行：coef 长度 n+2（x_1..x_n, u, v），mult 为 m 条来源的乘子。"""

    coef: tuple[Fraction, ...]
    rhs: Fraction
    mult: tuple[Fraction, ...]


@dataclass(frozen=True)
class Boundary:
    """规范化后的二维半平面 α·u + β·v ≤ rhs（α,β,rhs 互素整数）。"""

    index: int
    alpha: int
    beta: int
    rhs: int
    sources: tuple[tuple[int, Fraction], ...]  # 仅保留非零乘子 (原约束编号, λ)

    def relation(self) -> str:
        return f"{_pretty_uv(self.alpha, self.beta)} <= {self.rhs}"


@dataclass
class Envelope:
    boundaries: list[Boundary]
    bounded: bool
    recession: tuple[Fraction, Fraction] | None  # 无界时的非零见证方向
    n_source: int

    def as_dict(self, variable_names: Sequence[str],
                A: Sequence[Sequence[int]], b: Sequence[int],
                P: Sequence[int], Q: Sequence[int],
                labels: Sequence[str | None]) -> dict:
        src = []
        for i in range(self.n_source):
            src.append({
                "index": i,
                "label": labels[i],
                "coeffs": [str(v) for v in A[i]],
                "b": str(b[i]),
                "p": str(P[i]),
                "q": str(Q[i]),
            })
        return {
            "status": "envelope",
            "method": "fourier-motzkin/exact-rational",
            "variables": ["u", "v"],
            "current_variables": list(variable_names),
            "source_constraints": src,
            "perturbations": [[str(P[i]), str(Q[i])] for i in range(self.n_source)],
            "bounded": self.bounded,
            "recession_direction": (
                None if self.recession is None
                else [_frac_str(self.recession[0]), _frac_str(self.recession[1])]
            ),
            "boundaries": [self._boundary_dict(bd, A, b, P, Q, labels)
                           for bd in self.boundaries],
        }

    def _boundary_dict(self, bd: Boundary, A, b, P, Q, labels) -> dict:
        terms = []
        for i, lam in bd.sources:
            terms.append({
                "index": i,
                "label": labels[i],
                "multiplier": _frac_str(lam),
                "weighted_coeffs": [_frac_str(lam * Fraction(v)) for v in A[i]],
                "weighted_p": _frac_str(lam * Fraction(P[i])),
                "weighted_q": _frac_str(lam * Fraction(Q[i])),
                "weighted_rhs": _frac_str(lam * Fraction(b[i])),
            })
        return {
            "index": bd.index,
            "alpha": str(bd.alpha),
            "beta": str(bd.beta),
            "rhs": str(bd.rhs),
            "relation": bd.relation(),
            "sources": terms,
        }


def _frac_str(v: Fraction) -> str:
    if v.denominator == 1:
        return str(v.numerator)
    return f"{v.numerator}/{v.denominator}"


def _lcm(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return abs(a // gcd(a, b) * b)


def _normalize(row: _Row) -> _Row:
    """正向整数规范化：乘公分母后除以 gcd；方向（≤）保持不变。"""
    vals = list(row.coef) + [row.rhs]
    L = 1
    for v in vals:
        L = _lcm(L, v.denominator)
    ints = [int(v * L) for v in vals]
    g = 0
    for t in ints:
        g = gcd(g, abs(t))
    if g == 0:
        # 全行（含右端）为 0：0 <= 0，保持单位乘子
        return row
    factor = Fraction(L, g)  # 恒正
    new_coef = tuple(Fraction(t // g) for t in ints[:-1])
    new_rhs = Fraction(ints[-1] // g)
    new_mult = tuple(lam * factor for lam in row.mult)
    return _Row(new_coef, new_rhs, new_mult)


def _row_key(row: _Row) -> tuple:
    return tuple(int(v) for v in row.coef) + (int(row.rhs),)


def _dedup(rows: list[_Row]) -> list[_Row]:
    seen: set[tuple] = set()
    out: list[_Row] = []
    for r in rows:
        key = _row_key(r)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _tighten(rows: list[_Row]) -> list[_Row]:
    """按**原始法向方向**合并比例重复/平行不等式，仅保留最紧右端。

    FM 组合只产生非负合并，故法向成比例（含整数规范化后仍带公约数，
    如 2v≤25 与 v≤10）的行是同向半平面，右端最小者蕴含其余，可安全丢弃。
    方向取各系数（不含右端）除以其 gcd；全行系数为 0（0≤B）不参与合并。
    同时按键去重完全相同的行。
    """
    best: dict[tuple[int, ...], tuple[Fraction, _Row]] = {}
    consts: list[_Row] = []
    for r in rows:
        ints = [int(v) for v in r.coef]
        g = 0
        for t in ints:
            g = gcd(g, abs(t))
        if g == 0:
            consts.append(r)
            continue
        direction = tuple(t // g for t in ints)
        offset = Fraction(int(r.rhs), g)  # 原语方向下的（可能分数）右端
        old = best.get(direction)
        if old is None or offset < old[0]:
            best[direction] = (offset, r)
    kept = consts + [r for _, r in best.values()]
    return _dedup(kept)


def project(
    A: Sequence[Sequence[int]],
    b: Sequence[int],
    P: Sequence[int],
    Q: Sequence[int],
) -> Envelope:
    """精确投影。A/b 为原约束，P/Q 为每约束的两个整数扰动系数。"""
    m = len(b)
    n = len(A[0]) if m else 0
    if not 1 <= n <= 8:
        raise ValueError("电流变量个数必须在 1 到 8 之间")
    if not 1 <= m <= 48:
        raise ValueError("原约束条数必须在 1 到 48 之间")

    # 初始行 a_i·x + p_i·u + q_i·v ≤ b_i，来源乘子 e_i；先做正向规范化。
    rows: list[_Row] = []
    for i in range(m):
        coef = tuple(Fraction(v) for v in A[i]) + (
            Fraction(P[i]), Fraction(Q[i]))
        rows.append(_normalize(_Row(coef, Fraction(b[i]),
                                   tuple(Fraction(1 if j == i else 0)
                                         for j in range(m)))))
    rows = _tighten(rows)

    # ---- Fourier–Motzkin：依次消去 x_0 .. x_{n-1} ----
    for k in range(n):
        pos: list[_Row] = []
        neg: list[_Row] = []
        zero: list[_Row] = []
        for r in rows:
            c = r.coef[k]
            if c > 0:
                pos.append(r)
            elif c < 0:
                neg.append(r)
            else:
                zero.append(r)
        out = list(zero)
        seen = {_row_key(r) for r in zero}
        for rp in pos:
            cp = rp.coef[k]   # > 0
            for rn in neg:
                cn = rn.coef[k]  # < 0
                # (-cn)·rp + cp·rn：两个因子都是正整数，消去第 k 列且不反向
                fp, fn = -cn, cp
                coef = tuple(fp * rp.coef[j] + fn * rn.coef[j]
                             for j in range(n + 2))
                rhs = fp * rp.rhs + fn * rn.rhs
                mult = tuple(fp * rp.mult[j] + fn * rn.mult[j]
                             for j in range(m))
                new = _normalize(_Row(coef, rhs, mult))
                key = _row_key(new)
                if key not in seen:
                    seen.add(key)
                    out.append(new)
                    if len(out) > MAX_FM_ROWS:
                        raise ProjectionScaleError(
                            f"Fourier–Motzkin 中间约束数超过受理上限 "
                            f"{MAX_FM_ROWS}（消去第 {k + 1} 个电流变量时）"
                        )
        # 阶段末再按原始法向方向合并比例重复/平行行（含跨正负行配对的产物）
        rows = _tighten(out)

    # ---- 只剩 (u,v)：处理全零行、合并同方向平行边界 ----
    cand: dict[tuple[int, int], tuple[int, _Row]] = {}
    for r in rows:
        alpha = int(r.coef[-2])
        beta = int(r.coef[-1])
        rhs = int(r.rhs)
        if alpha == 0 and beta == 0:
            if rhs < 0:
                raise EnvelopeInfeasible(
                    f"投影出现 0 <= {rhs}；来源审计可行时不应发生")
            continue  # 0 <= B, B>=0：重言式
        key = (alpha, beta)
        old = cand.get(key)
        if old is None or rhs < old[0]:
            cand[key] = (rhs, r)

    ordered = sorted(cand.values(), key=lambda t: (t[1].coef[-2], t[1].coef[-1], t[0]))
    boundaries: list[Boundary] = []
    for k, (rhs, r) in enumerate(ordered):
        sources = tuple((i, lam) for i, lam in enumerate(r.mult) if lam != 0)
        bd = Boundary(k, int(r.coef[-2]), int(r.coef[-1]), rhs, sources)
        _verify_provenance(bd, A, b, P, Q)
        boundaries.append(bd)

    witness = _recession_witness(boundaries)
    return Envelope(
        boundaries=boundaries,
        bounded=witness is None,
        recession=witness,
        n_source=m,
    )


def _verify_provenance(
    bd: Boundary,
    A: Sequence[Sequence[int]],
    b: Sequence[int],
    P: Sequence[int],
    Q: Sequence[int],
) -> None:
    """运行时断言：边界确实是原约束的非负有理合并，电流列逐项消为 0。"""
    n = len(A[0])
    lams = [Fraction(0)] * len(b)
    for i, lam in bd.sources:
        assert lam > 0, f"边界 {bd.index} 来源乘子必须严格为正: {lam}"
        lams[i] = lam
    for j in range(n):
        s = sum((lams[i] * Fraction(A[i][j]) for i in range(len(b))),
                Fraction(0))
        assert s == 0, f"边界 {bd.index} 电流列 {j} 未消净: {s}"
    assert sum((lams[i] * Fraction(P[i]) for i in range(len(b))),
               Fraction(0)) == bd.alpha
    assert sum((lams[i] * Fraction(Q[i]) for i in range(len(b))),
               Fraction(0)) == bd.beta
    rhs = sum((lams[i] * Fraction(b[i]) for i in range(len(b))),
              Fraction(0))
    assert rhs == bd.rhs, f"边界 {bd.index} 合并右端不一致: {rhs} != {bd.rhs}"


def _recession_witness(
    boundaries: Sequence[Boundary],
) -> tuple[Fraction, Fraction] | None:
    """回收锥 {d: αd_u+βd_v ≤ 0} 含非零方向 ⇒ 无界；返回一个精确见证。

    纯二维精确判定，不经过 ≤48 行的单纯形（投影边界最多可达 MAX_FM_ROWS）：
    回收锥是过原点的齐次半平面交，若非 {0}，其任一极射线必落在某条约束的
    边界直线 α·d_u+β·d_v=0 上，即与某条边界法向 (α,β) 垂直。故只需逐一检验
    有限候选 (β,-α)、(-β,α)；全部不满足则回收锥为 {0}，包线有界。
    无任何边界（整平面）时直接给 (1,0)。
    """
    if not boundaries:
        return (Fraction(1), Fraction(0))
    for bd in boundaries:
        for d0, d1 in ((Fraction(bd.beta), Fraction(-bd.alpha)),
                       (Fraction(-bd.beta), Fraction(bd.alpha))):
            if all(o.alpha * d0 + o.beta * d1 <= 0 for o in boundaries):
                return (d0, d1)
    return None


# ------------------------- 坐标查询 -------------------------

@dataclass
class BoundaryEval:
    index: int
    alpha: int
    beta: int
    rhs: int
    u: Fraction
    v: Fraction
    lhs: Fraction
    residual: Fraction  # rhs - lhs；< 0 即违约

    @property
    def violated(self) -> bool:
        return self.residual < 0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "alpha": str(self.alpha),
            "beta": str(self.beta),
            "rhs": str(self.rhs),
            "lhs": _frac_str(self.lhs),
            "residual": _frac_str(self.residual),
            "violated": self.violated,
            "substitution": _substitution(
                self.alpha, self.beta, self.u, self.v, self.lhs),
        }


def evaluate(
    boundaries: Sequence[Boundary], u: Fraction, v: Fraction
) -> list[BoundaryEval]:
    """逐边界代入；residual<0 的第一条即首条违约边界（按规范化顺序）。"""
    evals = []
    for bd in boundaries:
        lhs = bd.alpha * u + bd.beta * v
        evals.append(BoundaryEval(bd.index, bd.alpha, bd.beta, bd.rhs,
                                  u, v, lhs, Fraction(bd.rhs) - lhs))
    return evals


def feasible_currents(
    variable_names: Sequence[str],
    A: Sequence[Sequence[int]],
    b: Sequence[int],
    P: Sequence[int],
    Q: Sequence[int],
    u: Fraction,
    v: Fraction,
) -> tuple[dict[str, Fraction], list[Fraction]]:
    """包线内点 ⇒ 精确求一组可复算电流，并回代给每条原约束算精确余量。

    把固定点 u,v 直接代入右端（整体乘公分母化为整数行，不增加求解变量）：
        D·a_i·x ≤ D·b_i − p_i·N_u·(D/den_u) − q_i·N_v·(D/den_v)
    """
    D = _lcm(u.denominator, v.denominator)
    nu = u.numerator * (D // u.denominator)
    nv = v.numerator * (D // v.denominator)
    cons = []
    for i in range(len(b)):
        coeffs = tuple(D * int(a) for a in A[i])
        rhs = D * b[i] - P[i] * nu - Q[i] * nv
        cons.append(Constraint(coeffs, rhs))
    res = solve(variable_names, cons)
    if res.__class__.__name__ != "FeasibleResult":
        raise EnvelopeInfeasible("内点查询却无电流解，投影实现有误")
    currents = dict(res.currents)
    residuals = []
    for i in range(len(b)):
        lhs_x = sum((Fraction(A[i][j]) * currents[name]
                     for j, name in enumerate(variable_names)), Fraction(0))
        # 原扰动右端 b_i - p_i·u - q_i·v 与 a_i·x 的差
        residual = Fraction(b[i]) - Fraction(P[i]) * u - Fraction(Q[i]) * v - lhs_x
        assert residual >= 0, f"原约束 {i} 回代余量为负: {residual}"
        residuals.append(residual)
    return currents, residuals


def violation_report(ev: BoundaryEval, u: Fraction, v: Fraction) -> dict:
    """首条违约边界的稳定标注：严格违反值与代入结果。"""
    gap = -ev.residual  # lhs - rhs，必 > 0
    assert gap > 0
    return {
        "boundary_index": ev.index,
        "alpha": str(ev.alpha),
        "beta": str(ev.beta),
        "rhs": str(ev.rhs),
        "lhs": _frac_str(ev.lhs),
        "gap": _frac_str(gap),
        "substitution": _substitution(ev.alpha, ev.beta, u, v, ev.lhs)
                        + f" > {ev.rhs}",
        "relation": f"{_pretty_uv(ev.alpha, ev.beta)} <= {ev.rhs}",
    }


def _pretty_uv(alpha: int, beta: int) -> str:
    parts = []
    for c, name in ((alpha, "u"), (beta, "v")):
        if c == 0:
            continue
        if not parts:
            if c == 1:
                parts.append(name)
            elif c == -1:
                parts.append(f"-{name}")
            else:
                parts.append(f"{c}*{name}")
        else:
            if c == 1:
                parts.append(f" + {name}")
            elif c == -1:
                parts.append(f" - {name}")
            elif c > 0:
                parts.append(f" + {c}*{name}")
            else:
                parts.append(f" - {-c}*{name}")
    return "".join(parts) if parts else "0"


def _substitution(alpha: int, beta: int,
                  u: Fraction | None, v: Fraction | None,
                  lhs: Fraction) -> str:
    """如 2·(1/3) - 1·(2) = -4/3；坐标缺省时只给左侧符号式。"""
    terms = []
    for c, val in ((alpha, u), (beta, v)):
        if c == 0 or val is None:
            continue
        terms.append(f"{c}*({_frac_str(val)})")
    left = " + ".join(terms).replace("+ -", "- ")
    if not terms:
        left = "0"
    return f"{left} = {_frac_str(lhs)}"
