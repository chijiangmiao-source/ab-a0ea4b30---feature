"""精确二维包线引擎测试（app.envelope）。

覆盖：
* Fourier–Motzkin 投影的形状：有界多边形、无界带、空边界=整平面、空投影；
* 比例重复边界合并、同方向平行边界取最紧右端、零系数行不产生重复；
* 每条边界的来源合并式独立重算（电流列逐项消为 0、乘子非负）；
* 无界方向见证满足全部边界法向，有界情形不返回见证；
* 坐标查询：内点给可复算电流且回代余量非负，外点给首条违约边界与严格违反值；
* 与独立 Fourier–Motzkin 可行性判定在随机小系统/随机有理点上交叉一致；
* 大整数全程精确；受理规模超限抛 ProjectionScaleError。
"""

from __future__ import annotations

import itertools
import random
from fractions import Fraction

import pytest

from app.envelope import (
    MAX_FM_ROWS,
    EnvelopeInfeasible,
    ProjectionScaleError,
    Boundary,
    evaluate,
    feasible_currents,
    project,
    violation_report,
)
from fourier_motzkin import fm_feasible


def relations(env):
    return [bd.relation() for bd in env.boundaries]


def check_boundary(bd: Boundary, A, b, P, Q):
    """独立重算来源合并式（不依赖引擎内部断言）。"""
    m, n = len(b), len(A[0])
    lam = [Fraction(0)] * m
    used = {i: l for i, l in bd.sources}
    assert set(used) == {i for i, l in bd.sources}
    for i, l in bd.sources:
        assert l > 0
        lam[i] = l
    for j in range(n):
        assert sum(lam[i] * A[i][j] for i in range(m)) == 0
    assert sum(lam[i] * P[i] for i in range(m)) == bd.alpha
    assert sum(lam[i] * Q[i] for i in range(m)) == bd.beta
    assert sum(lam[i] * b[i] for i in range(m)) == bd.rhs
    # 互素整数规范化：gcd(|alpha|,|beta|,rhs)=1
    from math import gcd
    assert gcd(gcd(abs(bd.alpha), abs(bd.beta)), abs(bd.rhs)) == 1


def check_envelope(env, A, b, P, Q):
    for bd in env.boundaries:
        check_boundary(bd, A, b, P, Q)
    if env.recession is not None:
        d0, d1 = env.recession
        assert (d0, d1) != (0, 0)
        for bd in env.boundaries:
            assert bd.alpha * d0 + bd.beta * d1 <= 0


def test_whole_plane_when_all_perturbations_zero():
    """零扰动列：投影是整个平面，不得错误封闭出任何边界。"""
    env = project([[1], [-1]], [5, 0], [0, 0], [0, 0])
    assert env.boundaries == []
    assert env.bounded is False
    assert env.recession is not None and env.recession != (0, 0)
    # 任意坐标（含巨大有理值）都可行且都能复算电流
    for u, v in [(10**30, -10**30), (Fraction(1, 7), Fraction(-9, 2))]:
        ev = evaluate(env.boundaries, Fraction(u), Fraction(v))
        assert not any(e.violated for e in ev)
        cur, res = feasible_currents(["x"], [[1], [-1]], [5, 0],
                                     [0, 0], [0, 0], Fraction(u), Fraction(v))
        assert all(r >= 0 for r in res)


def test_strip_projection():
    # x>=0; x+u<=10; x-u<=10  =>  -10 <= u <= 10，v 方向无界
    A = [[1], [1], [-1]]
    b = [10, 10, 0]
    env = project(A, b, [1, -1, 0], [0, 0, 0])
    assert relations(env) == ["-u <= 10", "u <= 10"]
    assert env.bounded is False
    d = env.recession
    assert d is not None and d[0] == 0 and d[1] != 0
    check_envelope(env, A, b, [1, -1, 0], [0, 0, 0])


def test_bounded_triangle():
    # x,y>=0; x-u<=0; y-v<=0; x+y+u+v<=1  => u>=0, v>=0, u+v<=1（三角形）
    A = [[-1, 0], [0, -1], [1, 0], [0, 1], [1, 1]]
    b = [0, 0, 0, 0, 1]
    P = [0, 0, -1, 0, 1]
    Q = [0, 0, 0, -1, 1]
    env = project(A, b, P, Q)
    assert env.bounded is True
    assert env.recession is None
    assert sorted(relations(env)) == ["-u <= 0", "-v <= 0", "u + v <= 1"]
    check_envelope(env, A, b, P, Q)

    # 内点（含顶点）可行并给电流
    for u, v in [(0, 0), (1, 0), (0, 1), (Fraction(1, 4), Fraction(1, 4))]:
        ev = evaluate(env.boundaries, u, v)
        assert not any(e.violated for e in ev)
        cur, res = feasible_currents(["x", "y"], A, b, P, Q, u, v)
        for i in range(len(b)):
            lhs = (A[i][0] * cur["x"] + A[i][1] * cur["y"]
                   + P[i] * u + Q[i] * v)
            assert lhs <= b[i]
        assert all(r >= 0 for r in res)

    # 外点：首条违约边界稳定、严格违反值正确
    ev = evaluate(env.boundaries, Fraction(1), Fraction(1))
    bad = [e for e in ev if e.violated]
    assert len(bad) == 1
    rep = violation_report(bad[0], Fraction(1), Fraction(1))
    assert rep["boundary_index"] == bad[0].index
    assert Fraction(rep["gap"]) == 1
    assert Fraction(rep["lhs"]) == 2 and int(rep["rhs"]) == 1


def test_proportional_duplicates_collapse():
    """比例重复边界只保留一条；同方向平行边界取最紧右端；零系数行不重复。"""
    # x+u<=2; x+u<=5; -x<=0; 2x+2u<=8（规范化 u<=4）；0<=0 型重言式
    A = [[1], [1], [-1], [2], [0]]
    b = [2, 5, 0, 8, 0]
    P = [1, 1, 0, 2, 0]
    Q = [0, 0, 0, 0, 0]
    env = project(A, b, P, Q)
    assert relations(env) == ["u <= 2"]
    check_envelope(env, A, b, P, Q)


def test_proportional_normals_with_fractional_offset():
    """整数规范化无法归一的同向平行行（v<=10 与 2v<=25）必须按原方向合并。

    2v<=25 规范化后 gcd(2,25)=1，不能除以 2；但它与 v<=10 同向，
    v<=10（右端 10 < 12.5）蕴含之，投影中不得残留冗余的 2v<=25。
    """
    # x<=100；-x+v<=-90  => 与 x<=100 合并 v<=10；
    # -x+2v<=-75        => 与 x<=100 合并 2v<=25（同向但更松，须丢弃）
    A = [[1], [-1], [-1]]
    b = [100, -90, -75]
    P = [0, 0, 0]
    Q = [0, 1, 2]
    assert fm_feasible([r[:] for r in A], b)  # 来源本身可行
    env = project(A, b, P, Q)
    assert relations(env) == ["v <= 10"]
    check_envelope(env, A, b, P, Q)


def test_provenance_with_fractional_multipliers():
    """跨多个电流变量消元会产生分数乘子，来源合并式仍须严格成立。"""
    # x+y+u<=1; x>=0; y>=0; x+y-u<=1  => -1<=u<=1
    A = [[1, 1], [-1, 0], [0, -1], [1, 1]]
    b = [1, 0, 0, 1]
    P = [1, 0, 0, -1]
    Q = [0, 0, 0, 0]
    env = project(A, b, P, Q)
    assert sorted(relations(env)) == ["-u <= 1", "u <= 1"]
    check_envelope(env, A, b, P, Q)
    # u<=1 由前三行合并（含两个非负乘子），电流列 (x,y) 同时消净
    bd_u = next(x for x in env.boundaries if x.alpha == 1)
    idxs = {i for i, _ in bd_u.sources}
    assert idxs == {0, 1, 2}


def test_infeasible_source_gives_empty_projection():
    """来源本身不可行：消元必出现 0 <= 负数，引擎报 EnvelopeInfeasible。"""
    with pytest.raises(EnvelopeInfeasible):
        project([[0], [1]], [-1, 5], [0, 1], [0, 0])


def test_huge_integers_exact():
    z = 10**60
    # x>=0; z*x + z*u <= z; z*x - z*u <= z => -1 <= u <= 1
    A = [[z], [z], [-z]]
    b = [z, z, 0]
    env = project(A, b, [z, -z, 0], [0, 0, 0])
    got = {(bd.alpha, bd.rhs) for bd in env.boundaries}
    assert got == {(-1, 1), (1, 1)}
    check_envelope(env, A, b, [z, -z, 0], [0, 0, 0])


def test_scale_exceeded_rejected():
    rng = random.Random(0)
    n, m = 8, 48
    A = [[rng.randint(-3, 3) for _ in range(n)] for _ in range(m)]
    b = [rng.randint(0, 50) for _ in range(m)]
    P = [rng.randint(-3, 3) for _ in range(m)]
    Q = [rng.randint(-3, 3) for _ in range(m)]
    with pytest.raises(ProjectionScaleError) as exc:
        project(A, b, P, Q)
    assert str(MAX_FM_ROWS) in str(exc.value)


# ---------------- 与独立 FM 判定交叉验证 ----------------

def _augmented_feasible(A, b, P, Q, u, v):
    """用 tests/fourier_motzkin 独立判断固定点 (u,v) 是否在投影内。

    (u,v) 已固定，扰动项移到右端：仅对电流列 x 做 FM 消元。
    """
    rows, rhs = [], []
    for i in range(len(b)):
        rows.append([Fraction(x) for x in A[i]])
        rhs.append(Fraction(b[i]) - P[i] * u - Q[i] * v)
    return fm_feasible(rows, rhs)


@pytest.mark.parametrize("seed", range(40))
def test_membership_matches_independent_fm(seed):
    rng = random.Random(1000 + seed)
    n = rng.randint(1, 3)
    m = rng.randint(2, 6)
    A = [[rng.randint(-2, 2) for _ in range(n)] for _ in range(m)]
    b = [rng.randint(-2, 4) for _ in range(m)]
    P = [rng.randint(-2, 2) for _ in range(m)]
    Q = [rng.randint(-2, 2) for _ in range(m)]
    names = [f"x{j}" for j in range(n)]

    if not fm_feasible([row[:] for row in A], b):
        # 不可行来源（服务层会拒绝，引擎仅防御性处理）：投影必不含原点；
        # 若矛盾行的扰动系数也为零则投影为空（抛 EnvelopeInfeasible）。
        try:
            env = project(A, b, P, Q)
        except EnvelopeInfeasible:
            return
        origin_bad = any(
            e.violated for e in evaluate(env.boundaries, Fraction(0), Fraction(0)))
        assert origin_bad, "来源不可行时 (0,0) 必须被某条投影边界排除"
        return

    env = project(A, b, P, Q)
    check_envelope(env, A, b, P, Q)
    vals = [Fraction(-2), Fraction(-1), Fraction(0), Fraction(1),
            Fraction(2), Fraction(1, 2), Fraction(-3, 2)]
    for u, v in itertools.product(vals, vals):
        expect = _augmented_feasible(A, b, P, Q, u, v)
        ev = evaluate(env.boundaries, u, v)
        got = not any(e.violated for e in ev)
        assert got == expect, (A, b, P, Q, u, v, expect, relations(env))
        if got:
            cur, res = feasible_currents(names, A, b, P, Q, u, v)
            for i in range(m):
                lhs = sum(A[i][j] * cur[names[j]] for j in range(n)) \
                    + P[i] * u + Q[i] * v
                assert lhs == b[i] - res[i]
                assert res[i] >= 0


def test_first_violation_is_stable_and_strict():
    """同一点多次/逆序查询，首条违约边界一致；违反值严格为正。"""
    A = [[-1, 0], [0, -1], [1, 0], [0, 1], [1, 1]]
    b = [0, 0, 0, 0, 1]
    P = [0, 0, -1, 0, 1]
    Q = [0, 0, 0, -1, 1]
    env = project(A, b, P, Q)
    u, v = Fraction(2), Fraction(0)  # 违反 u+v<=1（gap=1），不违反非负
    firsts = set()
    for _ in range(3):
        ev = evaluate(env.boundaries, u, v)
        bad = [e for e in ev if e.violated]
        assert bad and all(e.residual < 0 for e in bad)
        firsts.add(bad[0].index)
        rep = violation_report(bad[0], u, v)
        assert Fraction(rep["gap"]) == -bad[0].residual > 0
    assert firsts == {next(i for i, bd in enumerate(env.boundaries)
                           if bd.alpha == 1 and bd.beta == 1)}


def test_boundary_points_are_feasible_not_violated():
    """恰落在边界上的点余量为 0，必须判可行（严格违反只认 < 0）。"""
    A = [[1], [1], [-1]]
    b = [10, 10, 0]
    env = project(A, b, [1, -1, 0], [0, 0, 0])
    for u in (10, -10):
        ev = evaluate(env.boundaries, Fraction(u), Fraction(0))
        assert not any(e.violated for e in ev)
        assert min(e.residual for e in ev) == 0
