"""包线服务与冻结存储：来源准入、冻结/重放/冲突、坐标查询、不可改原审计。"""

from __future__ import annotations

import json
from fractions import Fraction

import pytest

from app.envelope import ProjectionScaleError
from app.service import AuditService, EnvelopeConflictError
from app.storage import AuditStore
from app.models import PayloadError


def make_service(tmp_path):
    return AuditService(AuditStore(str(tmp_path / "data")))


FEAS_AUDIT = {
    "audit_id": "run-feas",
    "variables": ["x", "y"],
    "constraints": [
        {"coeffs": [-1, 0], "b": 0, "label": "x>=0", "stable": True},
        {"coeffs": [0, -1], "b": 0, "label": "y>=0", "stable": True},
        {"coeffs": [1, 0], "b": 0, "label": "x-u<=0"},
        {"coeffs": [0, 1], "b": 0, "label": "y-v<=0"},
        {"coeffs": [1, 1], "b": 1, "label": "x+y+u+v<=1"},
    ],
}
# 与 test_envelope 的有界三角形同一结构：u>=0, v>=0, u+v<=1
PERTURBATIONS = [[0, 0], [0, 0], [-1, 0], [0, -1], [1, 1]]
ENV_REQ = {
    "envelope_id": "env-1",
    "source_audit_id": "run-feas",
    "perturbations": PERTURBATIONS,
}
INFEAS_AUDIT = {
    "audit_id": "run-inf",
    "variables": ["x"],
    "constraints": [{"coeffs": [0], "b": -1}],
}


@pytest.fixture()
def svc(tmp_path):
    s = make_service(tmp_path)
    s.audit(FEAS_AUDIT)
    return s


def test_create_requires_existing_source(svc):
    req = {**ENV_REQ, "source_audit_id": "ghost"}
    with pytest.raises(PayloadError, match="来源审计不存在"):
        svc.create_envelope(req)
    assert svc.fetch_envelope("env-1") is None


def test_create_requires_feasible_source(svc):
    svc.audit(INFEAS_AUDIT)
    req = {**ENV_REQ, "source_audit_id": "run-inf",
           "perturbations": [[0, 0]]}
    with pytest.raises(PayloadError, match="不可行"):
        svc.create_envelope(req)


def test_perturbation_count_must_match(svc):
    req = {**ENV_REQ, "perturbations": [[0, 0]]}  # 6 条约束只给 1 对
    with pytest.raises(PayloadError, match="必须等于原约束条数"):
        svc.create_envelope(req)


def test_reject_float_bool_and_big_ints(svc):
    for bad in [
        {**ENV_REQ, "perturbations": [[1.0, 0]] * 6},
        {**ENV_REQ, "perturbations": [[True, 0]] * 6},
        {**ENV_REQ, "perturbations": [["1", 0]] * 6},
        {**ENV_REQ, "perturbations": [[10**5000, 0]] * 6},
    ]:
        with pytest.raises(PayloadError):
            svc.create_envelope(bad)
    assert svc.fetch_envelope("env-1") is None


def test_freeze_replay_and_fetch(svc):
    rec, replayed = svc.create_envelope(ENV_REQ)
    assert replayed is False
    assert rec["result"]["bounded"] is True
    rels = [bd["relation"] for bd in rec["result"]["boundaries"]]
    assert sorted(rels) == ["-u <= 0", "-v <= 0", "u + v <= 1"]
    assert rec["result"]["source_audit_id"] == "run-feas"

    rec2, replayed2 = svc.create_envelope(json.loads(json.dumps(ENV_REQ)))
    assert replayed2 is True
    assert rec2["fingerprint"] == rec["fingerprint"]
    assert rec2["created_at"] == rec["created_at"]

    fetched = svc.fetch_envelope("env-1")
    assert fetched["fingerprint"] == rec["fingerprint"]


def test_changed_coefficients_conflict_and_frozen_kept(svc):
    rec, _ = svc.create_envelope(ENV_REQ)
    changed = json.loads(json.dumps(ENV_REQ))
    changed["perturbations"][4] = [2, 2]  # 改变系数
    with pytest.raises(EnvelopeConflictError):
        svc.create_envelope(changed)
    assert svc.fetch_envelope("env-1")["fingerprint"] == rec["fingerprint"]
    # 结果内容也不变
    rels = [bd["relation"] for bd in
            svc.fetch_envelope("env-1")["result"]["boundaries"]]
    assert sorted(rels) == ["-u <= 0", "-v <= 0", "u + v <= 1"]


def test_changed_source_conflicts_even_with_same_id(svc):
    """复用派生标识却改变来源必须被拒绝。"""
    other = {
        "audit_id": "run-other",
        "variables": ["x"],
        "constraints": [{"coeffs": [1], "b": 5}, {"coeffs": [-1], "b": 0}],
    }
    svc.audit(other)
    svc.create_envelope(ENV_REQ)
    req2 = {
        "envelope_id": "env-1",
        "source_audit_id": "run-other",
        "perturbations": [[1, 0], [-1, 0]],
    }
    with pytest.raises(EnvelopeConflictError):
        svc.create_envelope(req2)


def test_source_audit_not_modified_by_envelope(svc):
    before = svc.fetch("run-feas")
    svc.create_envelope(ENV_REQ)
    after = svc.fetch("run-feas")
    assert after == before


def test_query_inside_gives_recomputable_currents(svc):
    svc.create_envelope(ENV_REQ)
    ans = svc.query_point("env-1", {"u": "1/4", "v": "1/4"})
    assert ans["status"] == "feasible"
    x = Fraction(ans["currents"]["x"])
    y = Fraction(ans["currents"]["y"])
    # 代入扰动后系统: x>=0,y>=0,x<=10+u,y<=10+v,x+y+u+v<=1
    assert x >= 0 and y >= 0
    assert x + y + Fraction(1, 4) + Fraction(1, 4) <= 1
    assert all(Fraction(r["value"]) >= 0 for r in ans["residuals"])
    # 全部边界满足
    assert all(not c["violated"] for c in ans["boundary_checks"])


def test_query_integer_point_and_boundary_point(svc):
    svc.create_envelope(ENV_REQ)
    assert svc.query_point("env-1", {"u": 0, "v": 0})["status"] == "feasible"
    corner = svc.query_point("env-1", {"u": "1", "v": 0})
    assert corner["status"] == "feasible"
    assert any(c["residual"] == "0" for c in corner["boundary_checks"])


def test_query_outside_first_violation(svc):
    svc.create_envelope(ENV_REQ)
    ans = svc.query_point("env-1", {"u": "1", "v": "1"})
    assert ans["status"] == "infeasible"
    f = ans["first_violation"]
    assert Fraction(f["gap"]) == 1
    assert Fraction(f["lhs"]) == 2 and f["rhs"] == "1"
    # 稳定：第一条负余量边界即首条违约
    neg = [c["index"] for c in ans["boundary_checks"] if c["violated"]]
    assert f["boundary_index"] == neg[0]


def test_query_unknown_envelope_404(svc):
    with pytest.raises(LookupError):
        svc.query_point("nope", {"u": 0, "v": 0})


def test_query_bad_rational(svc):
    svc.create_envelope(ENV_REQ)
    for bad in [{"u": "1.5", "v": 0}, {"u": "1/0", "v": 0},
                {"u": 1.5, "v": 0}, {"u": True, "v": 0}, {"v": 1}]:
        with pytest.raises(PayloadError):
            svc.query_point("env-1", bad)


def test_scale_exceeded_propagates(svc, monkeypatch):
    """超过受理规模必须拒绝且不落盘。"""
    import app.service as service_mod
    def boom(*a, **k):
        raise ProjectionScaleError("超过受理上限 1024")
    monkeypatch.setattr(service_mod, "project", boom)
    with pytest.raises(ProjectionScaleError):
        svc.create_envelope(ENV_REQ)
    assert svc.fetch_envelope("env-1") is None
