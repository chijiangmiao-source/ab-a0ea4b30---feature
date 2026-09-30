"""精确有理 Fourier–Motzkin 投影：运行包线引擎。

工程师对已可行审计的每条原约束填两个整数扰动系数 (u_i, v_i)，在
功率漂移 p 与场强偏置 q 共同变化时，第 i 条约束变为

    a_i·x ≤ b_i + u_i·p + v_i·q
    ⇔ a_i·x − u_i·p − v_i·q ≤ b_i                       (标准形)

本引擎把全部电流自由变量 x 用 **Fourier–Motzkin 消元** 逐一代数消去，
一次性投影出全部可行扰动对 (p, q)，得到规范整数半平面集合

    α·p + β·q ≤ γ。

这是对二维联合可行域的*精确*投影：不抽样、不浮点、不只检查两个扰动轴。
每条半平面保留对原约束的非负有理乘子 λ（必有 Σλ_i·a_i = 0），
合并式可逐项复算。

关键性质:
  * 电流变量是自由变量（可正可负）。FM 消去自由变量 z 时，把行按
    z 的系数分成上界 (c>0: z≤U) 与下界 (c<0: z≥L)；存在 z 当且仅当
    所有 L≤U，故只需把上下界两两相加消去 z（与非负变量同一公式，
    无需额外界）。因此无界方向只是“该方向上没有生成边界”，
    引擎绝不会凭空补界或错误封闭。
  * 每个消元阶段都把行归一化到本原整数（公分母去分母、整体 gcd、
    首个非零系数正向），比例重复行按整数元组键去重，只保留一个
    合法来源代表元，不产生重复表示。
  * 零系数行直接传递；0≤非负 重言式丢弃；一旦出现 0≤负数，说明
    来源系统本身不可行（与“来源必须可行”矛盾，作为内部错误）。
  * 任意精度：全程 int / fractions.Fraction。中间行数或系数位宽
    超过受理能力时抛 :class:`EnvelopeScaleError`，拒绝出证，
    不做近似或截断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from math import gcd
from typing import Sequence

from .simplex import Constraint, solve

# 受理规模：任一消元阶段保留的不同行数上限、候选合并数上限、系数位宽上限。
# FM 最坏情况指数膨胀；超限即拒绝（见 EnvelopeScaleError），而不是抽样近似。
# 配合“每步选上下界乘积最小变量”的无损消元顺序，真实工程化稀疏/中密度
# 8 变量 48 约束系统（峰值约 1–2k 行）可被正常受理，病态稠密最坏情形被快速拒绝。
MAX_PROJECTION_ROWS = 4096
MAX_RAW_COMBINATIONS = 1_000_000
MAX_PROJECTION_BITS = 8192


class EnvelopeScaleError(Exception):
    """投影中间规模超出受理能力，拒绝生成包线。"""


class ProjectionContradiction(RuntimeError):
    """理论上不应发生：来源可行却消出 0≤负数（引擎实现有误）。"""


@dataclass(frozen=True)
class SourceConstraint:
    """一条原约束在 (p,q) 扰动下的形式 a·x ≤ b + u·p + v·q。"""

    coeffs: tuple[int, ...]
    b: int
    u: int
    v: int
    label: str | None = None


@dataclass(frozen=True)
class Halfplane:
    """一条规范二维半平面 α·p + β·q ≤ γ 及其来源乘子证据。"""

    index: int
    alpha: int
    beta: int
    gamma: int
    multipliers: tuple[Fraction, ...]  # 对每条原约束的非负有理乘子 λ_i

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.alpha, self.beta, self.gamma)


@dataclass
class EnvelopeProjection:
    variables: tuple[str, ...]
    sources: tuple[SourceConstraint, ...]
    halfplanes: list[Halfplane] = field(default_factory=list)
    # 每个消元阶段实际生成的合并式数量（含去重前），供审计与页面展示
    combinations_per_stage: list[int] = field(default_factory=list)
    # 实际采用的消元变量顺序（贪心启发式；确定可复现，不影响最终投影）
    elimination_order: list[int] = field(default_factory=list)

    @property
    def whole_plane(self) -> bool:
        """没有任何非平凡半平面 ⇔ 整个 (p,q) 平面都可行。"""
        return not self.halfplanes


# ---------------------------------------------------------------- 归一化

def _normalize_row(
    vec: Sequence[int],
    multipliers: Sequence[Fraction],
):
    """把整数不等式行归一化到本原整数（只允许**正**比例缩放）。

    vec 布局: [电流系数 n 个, p 系数, q 系数, 右端]。
    乘子随行同步缩放：行除以正数 k ⇒ 乘子也除以 k，非负性保持。

    绝不整体乘 -1：FM 中 (v,g) 与 (-v,-g) 是**相反的两个不同半平面**，
    翻行会把来源乘子变成负数、破坏非负合并证据，也会错误合并不同边界。
    正比例（gcd 去公因子）归一已保证每条半平面有唯一整数代表，
    比例重复行据此去重。

    返回 (归一化整数元组, 归一化乘子)；全零重言式 (0≤0) 返回 None。
    """
    g = 0
    for z in vec:
        g = gcd(g, abs(z))
    if g == 0:
        return None  # 0 ≤ 0
    if g == 1:
        return tuple(vec), tuple(multipliers)
    out_vec = tuple(z // g for z in vec)
    out_mus = tuple(mu / g for mu in multipliers)
    return out_vec, out_mus


def _bit_ok(vec: Sequence[int], multipliers: Sequence[Fraction]) -> bool:
    for z in vec:
        if abs(z).bit_length() > MAX_PROJECTION_BITS:
            return False
    for mu in multipliers:
        if (mu.numerator.bit_length() > MAX_PROJECTION_BITS
                or mu.denominator.bit_length() > MAX_PROJECTION_BITS):
            return False
    return True


# ---------------------------------------------------------------- 投影

def project(
    variable_names: Sequence[str],
    sources: Sequence[SourceConstraint],
) -> EnvelopeProjection:
    """把 a·x − u·p − v·q ≤ b 精确投影到 (p,q)。"""
    n = len(variable_names)
    m = len(sources)
    if not 1 <= n <= 8:
        raise ValueError("电流变量个数必须在 1 到 8 之间")
    if not 1 <= m <= 48:
        raise ValueError("原约束条数必须在 1 到 48 之间")

    width = n + 3  # [x_0..x_{n-1}, p, q, rhs]
    p_col, q_col, rhs_col = n, n + 1, n + 2

    # 行表: 规范整数行 -> 来源乘子 λ（初始为单位乘子）
    rows: dict[tuple[int, ...], tuple[Fraction, ...]] = {}
    for i, s in enumerate(sources):
        vec = [0] * width
        for j, a in enumerate(s.coeffs):
            vec[j] = a
        vec[p_col] = -s.u
        vec[q_col] = -s.v
        vec[rhs_col] = s.b
        mus = tuple(Fraction(1 if i == k else 0) for k in range(m))
        norm = _normalize_row(vec, mus)
        if norm is not None:
            nvec, nmus = norm
            # 初始常量行：来源可行时不允许 0≤负数；0≤正数直接丢弃
            if all(nvec[t] == 0 for t in range(n + 2)):
                if nvec[rhs_col] < 0:
                    raise ProjectionContradiction(
                        f"来源在原点已不可行: 0 ≤ {nvec[rhs_col]}"
                    )
                continue
            rows[nvec] = nmus

    combos_per_stage: list[int] = []
    eliminated_order: list[int] = []
    remaining = list(range(n))
    while remaining:
        # 无损启发式：FM 投影结果与消元顺序无关，但顺序决定中间行数。
        # 动态选“上界数×下界数”最小的变量（并列取编号最小，确定可复现），
        # 显著抑制结构化系统的膨胀；不会改变最终半平面集合，也不引入近似。
        best_j = -1
        best_key = None
        for j in remaining:
            n_up = n_lo = 0
            for vec in rows:
                c = vec[j]
                if c > 0:
                    n_up += 1
                elif c < 0:
                    n_lo += 1
            key = (n_up * n_lo, j)
            if best_key is None or key < best_key:
                best_key = key
                best_j = j
        j = best_j
        stage_no = len(combos_per_stage) + 1
        eliminated_order.append(j)
        remaining.remove(j)

        upper: list[tuple[tuple[int, ...], tuple[Fraction, ...]]] = []
        lower: list[tuple[tuple[int, ...], tuple[Fraction, ...]]] = []
        new_rows: dict[tuple[int, ...], tuple[Fraction, ...]] = {}
        for vec, mus in rows.items():
            c = vec[j]
            if c > 0:
                upper.append((vec, mus))
            elif c < 0:
                lower.append((vec, mus))
            else:
                new_rows[vec] = mus  # 零系数行直接传递

        # 去重前的组合数上界也设闸：即便后续大量重复，先生成海量乘积
        # 本身就是病态膨胀，直接拒绝而不是硬算。
        raw_combos = len(upper) * len(lower)
        if raw_combos > MAX_RAW_COMBINATIONS:
            raise EnvelopeScaleError(
                f"消元变量 x_{j}（第 {stage_no} 步）产生 "
                f"{len(upper)}×{len(lower)}={raw_combos} 个候选合并，"
                f"超过受理上限 {MAX_RAW_COMBINATIONS}，拒绝近似处理"
            )

        made = 0
        for uv, um in upper:
            cp = uv[j]  # > 0
            for lv, lm in lower:
                cn = lv[j]  # < 0
                # cp*z 项与 cn*z 项：上界乘 |cn|、下界乘 cp 后相加消去 z
                kp, kn = -cn, cp
                cvec = [kp * x + kn * y for x, y in zip(uv, lv)]
                cmus = tuple(kp * x + kn * y for x, y in zip(um, lm))
                made += 1
                norm = _normalize_row(cvec, cmus)
                if norm is None:
                    continue  # 合并出 0≤0
                nvec, nmus = norm
                # 电流与 (p,q) 系数全零 ⇔ 常量行
                if all(nvec[t] == 0 for t in range(n + 2)):
                    if nvec[rhs_col] < 0:
                        # Σ λ a = 0 而 Σ λ b < 0：来源不可行的 Farkas 证书，
                        # 来源已被上层断言可行，出现即引擎错误。
                        raise ProjectionContradiction(
                            "消元得到 0 ≤ "
                            f"{nvec[rhs_col]}（乘子 {nmus}）"
                        )
                    continue  # 0 ≤ 非负：丢弃
                if not _bit_ok(nvec, nmus):
                    raise EnvelopeScaleError(
                        "投影系数位宽超出受理能力，拒绝近似处理"
                    )
                if nvec not in new_rows:
                    if len(new_rows) >= MAX_PROJECTION_ROWS:
                        raise EnvelopeScaleError(
                            f"消元变量 x_{j}（第 {stage_no} 步）后不同行数超过 "
                            f"{MAX_PROJECTION_ROWS}，超出受理规模"
                        )
                    new_rows[nvec] = nmus  # 比例重复行：保留唯一代表
        rows = new_rows
        combos_per_stage.append(made)

    # 消元完毕：所有电流系数必须严格为零
    halfplanes: list[Halfplane] = []
    for vec, mus in rows.items():
        if any(vec[j] != 0 for j in range(n)):
            raise ProjectionContradiction("消元后仍残留电流变量系数")
        alpha, beta, gamma = vec[p_col], vec[q_col], vec[rhs_col]
        _verify_provenance(sources, mus, alpha, beta, gamma)
        halfplanes.append(
            Halfplane(index=0, alpha=alpha, beta=beta, gamma=gamma,
                      multipliers=mus)
        )
    halfplanes.sort(key=lambda h: h.key)
    halfplanes = [
        Halfplane(index=k, alpha=h.alpha, beta=h.beta, gamma=h.gamma,
                  multipliers=h.multipliers)
        for k, h in enumerate(halfplanes)
    ]
    return EnvelopeProjection(
        variables=tuple(variable_names),
        sources=tuple(sources),
        halfplanes=halfplanes,
        combinations_per_stage=combos_per_stage,
        elimination_order=eliminated_order,
    )


def _verify_provenance(
    sources: Sequence[SourceConstraint],
    mus: Sequence[Fraction],
    alpha: int,
    beta: int,
    gamma: int,
) -> None:
    """用原始约束独立重算合并式，运行时断言证据与规范行严格一致。"""
    n = len(sources[0].coeffs)
    cur = [Fraction(0) for _ in range(n)]
    pc = qc = rhs = Fraction(0)
    for mu, s in zip(mus, sources):
        if mu < 0:
            raise ProjectionContradiction("来源乘子为负")
        for j, a in enumerate(s.coeffs):
            cur[j] += mu * a
        pc += mu * (-s.u)
        qc += mu * (-s.v)
        rhs += mu * s.b
    if any(c != 0 for c in cur):
        raise ProjectionContradiction(f"合并式电流系数未消净: {cur}")
    # 有理合并式必须是整数规范行的严格正比例
    if alpha != 0:
        ratio = pc / alpha
    elif beta != 0:
        ratio = qc / beta
    else:
        # 常量行在投影中已被丢弃，半平面的 (p,q) 系数不应全零
        raise ProjectionContradiction("半平面的 (p,q) 系数全零")
    if ratio <= 0:
        raise ProjectionContradiction("规范行与合并式比例非正")
    if pc != alpha * ratio or qc != beta * ratio or rhs != gamma * ratio:
        raise ProjectionContradiction(
            f"合并式 ({pc}, {qc}, {rhs}) 与规范行 ({alpha}, {beta}, {gamma}) 不成比例"
        )


# ---------------------------------------------------------------- 查询

def first_violation(
    projection: EnvelopeProjection, p: Fraction, q: Fraction
) -> Halfplane | None:
    """按冻结的规范顺序返回首条被严格违反的边界；点在包线内返回 None。

    等号成立视为满足（闭半平面），只有 α·p + β·q > γ 才算违约。
    """
    for h in projection.halfplanes:
        if h.alpha * p + h.beta * q > h.gamma:
            return h
    return None


def witness_currents(
    projection: EnvelopeProjection, p: Fraction, q: Fraction
) -> tuple[dict[str, Fraction], list[Fraction]]:
    """给定点在包线内时，求一组可复算电流并返回各约束精确余量。

    直接调用精确 Phase-I 单纯形解具体化系统
        a_i·x ≤ b_i + u_i·p + v_i·q  （右端为精确分数），
    并独立回代核验每条余量 ≥ 0。点若在投影外则无解，理论上不会发生
    （调用方已用半平面判定），出现即引擎错误。
    """
    names = projection.variables
    cons: list[Constraint] = []
    rhs_values: list[Fraction] = []
    for s in projection.sources:
        rhs = Fraction(s.b) + s.u * p + s.v * q
        rhs_values.append(rhs)
        cons.append(Constraint(coeffs=s.coeffs, b=rhs, label=s.label))
    result = solve(names, cons)
    if result.__class__.__name__ != "FeasibleResult":
        raise ProjectionContradiction(
            "半平面判定可行，但具体电流系统无解：投影实现有误"
        )
    currents = result.currents
    margins: list[Fraction] = []
    for s, rhs in zip(projection.sources, rhs_values):
        lhs = sum(
            (Fraction(s.coeffs[j]) * currents[name]
             for j, name in enumerate(names)),
            Fraction(0),
        )
        margin = rhs - lhs
        if margin < 0:
            raise ProjectionContradiction(
                f"见证电流违反原约束: {lhs} > {rhs}"
            )
        margins.append(margin)
    return currents, margins
