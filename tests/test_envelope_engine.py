"""精确 Fourier–Motzkin 投影引擎测试。

覆盖：
* 投影结果与“具体 (p,q) 点直接用 FM/单纯形判可行”在大量有理点上一致
  （引擎不抽样；这里测试用枚举/随机点做独立交叉核验）；
* 每条半平面的非负乘子独立复算：λ≥0、Σλ·a=0、比例等于规范整数行；
* 全平面、无界方向不被错误封闭、比例重复边界只保留一个表示、
  零系数电流行、本原 gcd 归一、自由变量两侧界、3 变量链式消元；
* 有理坐标严格判定（等号可行，越过边界严格为正的违反值）；
* 超受理规模（行数/位宽）抛 EnvelopeScaleError，拒绝近似。
"""

from __future__ import annotations

import random

import pytest

from app import envelope as env_mod
from app.envelope import (
    EnvelopeScaleError,
    SourceConstraint,
    first_violation,
    project,
    witness_currents,
)

from fourier_motzkin import fm_feasible


def S(coeffs, b, u=0, v=0, label=None):
    return SourceConstraint(tuple(coeffs), b, u, v, label)


def check_halfplanes(proj):
    """独立复算每条半平面的来源合并式与规范行。"""
    sources = proj.sources
    keys = []
    for h in proj.halfplanes:
        keys.append(h.key)
        cur = [0] * len(sources[0].coeffs)
        pc = qc = rc = 0
        # 用 Fraction 精确重算
        from fractions import Fraction
        cur = [Fraction(0) for _ in cur]
        pc = qc = rc = Fraction(0)
        for lam, s in zip(h.multipliers, sources):
            assert lam >= 0, "来源乘子必须非负"
            for j, a in enumerate(s.coeffs):
                cur[j] += lam * a
            pc += lam * (-s.u)
            qc += lam * (-s.v)
            rc += lam * s.b
        assert all(c == 0 for c in cur), f"电流系数必须消净: {cur}"
        # (pc,qc,rc) = t*(alpha,beta,gamma), t>0
        a, b, g = h.alpha, h.beta, h.gamma
        if a != 0:
            t = pc / a
        elif b != 0:
            t = qc / b
        else:
            pytest.fail("半平面 (p,q) 系数全零，常量行不应进入投影")
        assert t > 0
        assert pc == a * t and qc == b * t and rc == g * t
        # 行必须本原（gcd=1）
        from math import gcd
        assert gcd(gcd(abs(a), abs(b)), abs(g)) == 1
    # 无重复表示
    assert len(keys) == len(set(keys)), f"出现重复半平面: {keys}"
    # 索引与规范排序一致
    assert keys == sorted(keys)


def point_feasible_via_fm(sources, p, q):
    """独立方法：把 (p,q) 代入后对电流系统做 FM 判定。"""
    A = [list(s.coeffs) for s in sources]
    b = [s.b + s.u * p + s.v * q for s in sources]
    from fractions import Fraction
    b = [Fraction(x) for x in b]
    # FM 测试工具只吃整数右端；乘公分母不改变可行性
    den = 1
    for x in b:
        den = den // __import__("math").gcd(den, x.denominator) * x.denominator
    b_int = [int(x * den) for x in b]
    return fm_feasible(A, b_int)


def test_box_projection_basic():
    # 0<=x<=2, 0<=y<=3；上限 x<=2+p, y<=3+q
    proj = project(["x", "y"], [
        S([1, 0], 2, 1, 0), S([0, 1], 3, 0, 1),
        S([-1, 0], 0), S([0, -1], 0),
    ])
    assert [h.key for h in proj.halfplanes] == [(-1, 0, 2), (0, -1, 3)]
    check_halfplanes(proj)


def test_whole_plane_when_no_perturbation():
    proj = project(["x"], [S([1], 2), S([-1], 0)])
    assert proj.whole_plane and proj.halfplanes == []
    # 任意方向都可行（不被错误封闭）
    for p, q in [(10**30, 0), (-10**30, 7), (0, -10**30)]:
        from fractions import Fraction
        assert first_violation(proj, Fraction(p), Fraction(q)) is None


def test_unbounded_direction_not_closed():
    # 只约束 p（x<=1+p, x>=0），q 方向必须保持无界
    proj = project(["x"], [S([1], 1, 1, 0), S([-1], 0)])
    assert [h.key for h in proj.halfplanes] == [(-1, 0, 1)]
    from fractions import Fraction
    assert first_violation(proj, Fraction(0), Fraction(10**50)) is None
    assert first_violation(proj, Fraction(0), Fraction(-10**50)) is None
    check_halfplanes(proj)


def test_proportional_duplicates_single_representation():
    # x<=1+p 与 2x<=2+2p 是比例重复；分别与 x>=0 合并不得产生重复边界
    proj = project(["x"], [
        S([1], 1, 1, 0, "a"),
        S([2], 2, 2, 0, "2a"),
        S([-1], 0),
    ])
    assert [h.key for h in proj.halfplanes] == [(-1, 0, 1)]
    check_halfplanes(proj)


def test_gcd_primitivity_normalization():
    # 2x<=4+2p+2q 与 -x<=0 合并 => 0<=4+2p+2q => -p-q<=2（本原）
    proj = project(["x"], [S([2], 4, 2, 2), S([-1], 0)])
    assert [h.key for h in proj.halfplanes] == [(-1, -1, 2)]
    check_halfplanes(proj)


def test_zero_current_coefficient_row():
    # 0*x <= 1-p（即 p<=1）直接成为边界；x<=5 在自由 x 下不产生约束
    proj = project(["x"], [S([0], 1, -1, 0), S([1], 5)])
    assert [h.key for h in proj.halfplanes] == [(1, 0, 1)]
    check_halfplanes(proj)


def test_free_var_both_sides_three_var_chain():
    srcs = [
        S([1, -1, 0], 0),
        S([0, 1, -1], 0),
        S([0, 0, 1], 1, 1, 0),   # x3 <= 1+p
        S([-1, 0, 0], 0),
        S([0, -1, 0], 0),
        S([0, 0, -1], 0),
    ]
    proj = project(["x1", "x2", "x3"], srcs)
    assert [h.key for h in proj.halfplanes] == [(-1, 0, 1)]
    check_halfplanes(proj)


def test_boundary_equality_is_feasible_strict_excess_positive():
    from fractions import Fraction
    proj = project(["x"], [S([1], 1, 1, 0), S([-1], 0)])  # p >= -1
    assert first_violation(proj, Fraction(-1), Fraction(0)) is None
    v = first_violation(proj, Fraction(-3, 2), Fraction(0))
    assert v.key == (-1, 0, 1)
    lhs = v.alpha * Fraction(-3, 2)
    assert lhs == Fraction(3, 2)
    assert lhs - v.gamma == Fraction(1, 2)  # 严格违反值 > 0


def test_rational_diagonal_boundary_exact():
    # 2x <= 2 + p + q, x>=0 => -p-q <= 2；p=q=-1 恰在边界
    from fractions import Fraction
    proj = project(["x"], [S([2], 2, 1, 1), S([-1], 0)])
    assert [h.key for h in proj.halfplanes] == [(-1, -1, 2)]
    assert first_violation(proj, Fraction(-1), Fraction(-1)) is None
    # p=-1/3, q=-1 时代入 4/3 <= 2 可行；p=-2,q=-1 时 3<=2 违约
    assert first_violation(proj, Fraction(-1, 3), Fraction(-1)) is None
    v = first_violation(proj, Fraction(-2), Fraction(-1))
    assert v is not None and (v.alpha * -2 + v.beta * -1 - v.gamma) == 1


def test_witness_currents_satisfy_concrete_constraints():
    from fractions import Fraction
    proj = project(["x", "y"], [
        S([1, 0], 2, 1, 0), S([0, 1], 3, 0, 1),
        S([-1, 0], 0), S([0, -1], 0), S([1, 1], 4),
    ])
    p, q = Fraction(-1, 2), Fraction(1, 3)
    assert first_violation(proj, p, q) is None
    currents, margins = witness_currents(proj, p, q)
    for s, margin in zip(proj.sources, margins):
        lhs = sum(Fraction(s.coeffs[j]) * currents[name]
                  for j, name in enumerate(proj.variables))
        rhs = Fraction(s.b) + s.u * p + s.v * q
        assert lhs <= rhs and margin == rhs - lhs and margin >= 0


@pytest.mark.parametrize("seed", range(40))
def test_membership_matches_independent_fm(seed):
    """投影内/外判定必须与把 (p,q) 具体化后独立 FM 判定完全一致。"""
    from fractions import Fraction
    rng = random.Random(seed)
    n = rng.randint(1, 3)
    m = rng.randint(2, 6)
    names = [f"x{j}" for j in range(n)]
    # 生成一个基线可行的来源
    for _try in range(100):
        A = [[rng.randint(-2, 2) for _ in range(n)] for _ in range(m)]
        b = [rng.randint(-2, 3) for _ in range(m)]
        if fm_feasible(A, b):
            break
    else:
        pytest.skip("未抽到可行基线")
    u = [rng.randint(-1, 1) for _ in range(m)]
    v = [rng.randint(-1, 1) for _ in range(m)]
    sources = [S(row, rhs, ui, vi) for row, rhs, ui, vi in zip(A, b, u, v)]
    proj = project(names, sources)
    check_halfplanes(proj)

    # 枚举/随机一批有理点交叉判定
    pts = [(Fraction(pi), Fraction(qi))
           for pi in range(-3, 4) for qi in range(-3, 4)]
    pts += [
        (Fraction(rng.randint(-5, 5), rng.randint(1, 3)),
         Fraction(rng.randint(-5, 5), rng.randint(1, 3)))
        for _ in range(20)
    ]
    for p, q in pts:
        inside_proj = first_violation(proj, p, q) is None
        inside_fm = point_feasible_via_fm(sources, p, q)
        assert inside_proj == inside_fm, (
            f"投影判定 {inside_proj} 与 FM {inside_fm} 在 (p,q)=({p},{q}) 不一致"
        )
        if inside_proj:
            currents, margins = witness_currents(proj, p, q)
            for s, margin in zip(sources, margins):
                lhs = sum(Fraction(s.coeffs[j]) * currents[name]
                          for j, name in enumerate(names))
                assert lhs <= Fraction(s.b) + s.u * p + s.v * q
                assert margin >= 0


def test_first_violation_picks_lowest_index():
    # 两条边界，外点同时违反，必须稳定返回规范顺序中的首条
    from fractions import Fraction
    # 两条零电流系数边界 0·x ≤ −p (p≤0) 与 0·x ≤ −q (q≤0)
    proj = project(["x"], [
        S([0], 0, -1, 0),   # p <= 0
        S([0], 0, 0, -1),   # q <= 0
    ])
    keys = [h.key for h in proj.halfplanes]
    assert keys == [(0, 1, 0), (1, 0, 0)]
    v = first_violation(proj, Fraction(1), Fraction(1))
    assert v.index == 0 and v.key == (0, 1, 0)


def test_scale_row_limit_rejected(monkeypatch):
    monkeypatch.setattr(env_mod, "MAX_PROJECTION_ROWS", 2)
    with pytest.raises(EnvelopeScaleError):
        project(["x", "y"], [
            S([1, 0], 1, 1, 0), S([0, 1], 1, 0, 1),
            S([-1, 0], 0), S([0, -1], 0),
        ])


def test_scale_raw_combo_limit_rejected(monkeypatch):
    # 候选合并数闸门必须在真正生成海量乘积之前拒绝
    monkeypatch.setattr(env_mod, "MAX_RAW_COMBINATIONS", 0)
    with pytest.raises(EnvelopeScaleError):
        project(["x"], [S([1], 1, 1, 0), S([-1], 0)])


def test_elimination_order_deterministic_and_covers_all_vars():
    # 盒界 + 少量稀疏耦合（在受理规模内），重点核对顺序确定且覆盖全部变量
    n = 5
    srcs = []
    for j in range(n):
        srcs.append(S(tuple(1 if k == j else 0 for k in range(n)),
                      10, 1 if j == 0 else 0, 1 if j == 1 else 0))
        srcs.append(S(tuple(-1 if k == j else 0 for k in range(n)), 0))
    for i in range(n - 1):
        row = [0] * n
        row[i], row[i + 1] = 1, -1
        srcs.append(S(tuple(row), 5))
    names = [f"x{j}" for j in range(n)]
    p1 = project(names, srcs)
    p2 = project(names, srcs)
    assert sorted(p1.elimination_order) == list(range(n))
    assert p1.elimination_order == p2.elimination_order  # 确定可复现
    assert [h.key for h in p1.halfplanes] == [h.key for h in p2.halfplanes]
    assert len(p1.combinations_per_stage) == n


def test_scale_bit_limit_rejected(monkeypatch):
    monkeypatch.setattr(env_mod, "MAX_PROJECTION_BITS", 4)
    with pytest.raises(EnvelopeScaleError):
        project(["x"], [S([1], 1000, 1000, 0), S([-1], 0)])


def test_no_floating_point_in_projection():
    proj = project(["x"], [S([3], 1, 1, 1), S([-3], 0)])
    for h in proj.halfplanes:
        assert isinstance(h.alpha, int) and isinstance(h.beta, int)
        assert isinstance(h.gamma, int)
        for mu in h.multipliers:
            assert mu.__class__.__name__ == "Fraction"
    # 3*(3x<=1+p+q)+3*(-3x<=0) => 0<=3+3p+3q, gcd 归一 => -p-q<=1（无分数）
    assert [h.key for h in proj.halfplanes] == [(-1, -1, 1)]


def test_rational_multipliers_kept_exact():
    # 2x<=1+p 与 -x<=0 合并 => 0<=1+p（整数系数，但乘子 λ=1/2 的等价构造）
    # 用 2x<=1 与 -2x<=0：kp=2,kn=2 => 0<=2+2p => -p<=1；
    # 这里验证带分数乘子也能精确表示：x<=1 与 -2x<=0 => kp=2,kn=1
    proj = project(["x"], [S([1], 1, 1, 0), S([-2], 0)])
    # 2*(x<=1+p)+1*(-2x<=0) = 0<=2+2p => -p<=1；首行乘子 2/(gcd2)=1，次行 1/2
    assert [h.key for h in proj.halfplanes] == [(-1, 0, 1)]
    h = proj.halfplanes[0]
    from fractions import Fraction
    assert h.multipliers == (Fraction(1), Fraction(1, 2))
    check_halfplanes(proj)
