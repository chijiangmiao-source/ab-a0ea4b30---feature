"""包线服务编排测试：冻结/重放/冲突/拒绝保护/原审计不可改写。"""

from __future__ import annotations

import json

import pytest

from app.envelope_service import EnvelopeRejectedError, EnvelopeService
from app.simplex import solve
from app.storage import (
    AuditStore,
    EnvelopeConflictError,
    EnvelopeStore,
    fingerprint,
)


def make_services(tmp_path):
    d = str(tmp_path / "data")
    audit = AuditStore(d)
    return audit, EnvelopeService(audit, EnvelopeStore(d))


def freeze_audit(store, audit_id, variables, constraints, status=None):
    from app.simplex import Constraint
    cons = [Constraint(tuple(c["coeffs"]), c["b"]) for c in constraints]
    result = solve(variables, cons).as_dict()
    canonical = {
        "audit_id": audit_id,
        "variables": list(variables),
        "constraints": [
            {"coeffs": list(c["coeffs"]), "b": c["b"],
             "label": c.get("label"), "stable": c.get("stable", False)}
            for c in constraints
        ],
    }
    record, _ = store.submit(audit_id, canonical, result)
    return record


FEAS = {
    "variables": ["x", "y"],
    "constraints": [
        {"coeffs": [1, 0], "b": 2, "label": "x-up"},
        {"coeffs": [0, 1], "b": 3, "label": "y-up"},
        {"coeffs": [-1, 0], "b": 0},
        {"coeffs": [0, -1], "b": 0},
    ],
}


def env_payload(eid="env-1", sid="audit-feas", perturbations=None):
    if perturbations is None:
        perturbations = [
            {"u": 1, "v": 0}, {"u": 0, "v": 1},
            {"u": 0, "v": 0}, {"u": 0, "v": 0},
        ]
    return {"envelope_id": eid, "source_audit_id": sid,
            "perturbations": perturbations}


@pytest.fixture()
def services(tmp_path):
    audit, svc = make_services(tmp_path)
    freeze_audit(audit, "audit-feas", FEAS["variables"], FEAS["constraints"])
    freeze_audit(audit, "audit-inf", ["x"],
                 [{"coeffs": [1], "b": 0}, {"coeffs": [-1], "b": -1}])
    return audit, svc


def test_create_freezes_halfplanes(services):
    _, svc = services
    rec, replayed = svc.create(env_payload())
    assert replayed is False
    keys = [(h["alpha"], h["beta"], h["gamma"])
            for h in rec["projection"]["halfplanes"]]
    assert keys == [(-1, 0, 2), (0, -1, 3)]
    assert rec["projection"]["method"] == "exact-rational/fourier-motzkin"
    assert rec["source_audit_id"] == "audit-feas"
    assert rec["fingerprint"].startswith("sha256:")


def test_replay_identical_returns_same_record(services):
    _, svc = services
    r1, _ = svc.create(env_payload())
    r2, replayed = svc.create(json.loads(json.dumps(env_payload())))
    assert replayed is True
    assert r1["fingerprint"] == r2["fingerprint"]
    assert r1["created_at"] == r2["created_at"]


def test_change_coefficients_conflicts_and_preserves(services):
    _, svc = services
    r1, _ = svc.create(env_payload())
    changed = json.loads(json.dumps(env_payload()))
    changed["perturbations"][0]["u"] = 5
    with pytest.raises(EnvelopeConflictError):
        svc.create(changed)
    fetched = svc.fetch("env-1")
    assert fetched["fingerprint"] == r1["fingerprint"]
    keys = [(h["alpha"], h["beta"], h["gamma"])
            for h in fetched["projection"]["halfplanes"]]
    assert keys == [(-1, 0, 2), (0, -1, 3)]


def test_change_source_conflicts_even_same_id(services):
    audit, svc = services
    # 同 envelope_id，但改指到另一条来源（内容不同 → 来源指纹不同）
    freeze_audit(audit, "audit-other", ["x", "y"], [
        {"coeffs": [1, 0], "b": 99},
        {"coeffs": [0, 1], "b": 99},
        {"coeffs": [-1, 0], "b": 0},
        {"coeffs": [0, -1], "b": 0},
    ])
    svc.create(env_payload(sid="audit-feas"))
    with pytest.raises(EnvelopeConflictError):
        svc.create(env_payload(sid="audit-other"))
    assert svc.fetch("env-1")["source_audit_id"] == "audit-feas"


def test_source_not_found_rejected(services):
    _, svc = services
    with pytest.raises(EnvelopeRejectedError) as ei:
        svc.create(env_payload(sid="ghost"))
    assert ei.value.code == "source_not_found"
    assert svc.fetch("env-1") is None


def test_infeasible_source_rejected(services):
    _, svc = services
    p = env_payload(sid="audit-inf", perturbations=[
        {"u": 0, "v": 0}, {"u": 0, "v": 0}])
    with pytest.raises(EnvelopeRejectedError) as ei:
        svc.create(p)
    assert ei.value.code == "source_infeasible"
    assert svc.fetch("env-1") is None


def test_invalid_payload_leaves_no_envelope(services):
    _, svc = services
    bad = env_payload()
    bad["perturbations"] = [{"u": 1.5, "v": 0}] * 4
    with pytest.raises(ValueError):
        svc.create(bad)
    assert svc.fetch("env-1") is None
    # 随后合法提交可正常首次冻结
    rec, replayed = svc.create(env_payload())
    assert replayed is False


def test_perturbation_count_must_match_source(services):
    _, svc = services
    bad = env_payload(perturbations=[{"u": 0, "v": 0}])  # 少 3 对
    with pytest.raises(ValueError, match="严格相等"):
        svc.create(bad)
    bad2 = env_payload(perturbations=[{"u": 0, "v": 0}] * 5)  # 多 1 对
    with pytest.raises(ValueError, match="严格相等"):
        svc.create(bad2)


def test_creating_envelope_does_not_modify_audit(services):
    audit, svc = services
    before = json.dumps(audit.get("audit-feas"), sort_keys=True)
    svc.create(env_payload())
    svc.create(json.loads(json.dumps(env_payload())))  # 重放
    with pytest.raises(EnvelopeConflictError):
        changed = env_payload()
        changed["perturbations"][0]["u"] = 9
        svc.create(changed)
    after = json.dumps(audit.get("audit-feas"), sort_keys=True)
    assert before == after


def test_query_inside_outside_and_evidence(services):
    from fractions import Fraction
    _, svc = services
    svc.create(env_payload())
    inside = svc.query("env-1", {"p": "-1/2", "q": "1/3"})
    assert inside["feasible"] is True
    currents = {k: Fraction(v) for k, v in inside["currents"].items()}
    A = [c["coeffs"] for c in FEAS["constraints"]]
    for i, m in enumerate(inside["margins"]):
        rhs = Fraction(m["rhs"])
        lhs = sum(A[i][j] * currents[k]
                  for j, k in enumerate(currents))
        assert lhs <= rhs
        assert Fraction(m["residual"]) == rhs - lhs

    outside = svc.query("env-1", {"p": -3, "q": 0})
    assert outside["feasible"] is False
    v = outside["violation"]
    assert (v["alpha"], v["beta"], v["gamma"]) == (-1, 0, 2)
    assert v["substitution"] == "3"
    assert v["strict_excess"] == "1"  # 严格违反值
    # 证据电流系数全零，合并式与规范行成正比例
    d = v["derivation"]
    assert all(x == "0" for x in d["combined_coeffs"])
    assert d["combined_p"] == "-1" and d["combined_rhs"] == "2"

    # 边界等号可行
    boundary = svc.query("env-1", {"p": -2, "q": -3})
    assert boundary["feasible"] is True


def test_query_unknown_envelope(services):
    _, svc = services
    with pytest.raises(EnvelopeRejectedError) as ei:
        svc.query("nope", {"p": 0, "q": 0})
    assert ei.value.code == "envelope_not_found"


def test_query_bad_coordinate(services):
    _, svc = services
    svc.create(env_payload())
    for bad in [{"p": 1.5, "q": 0}, {"p": "1/0", "q": 0},
                {"p": "x", "q": 0}, {"q": 0}]:
        with pytest.raises(ValueError):
            svc.query("env-1", bad)


def test_whole_plane_envelope_query_anywhere(services):
    _, svc = services
    p = env_payload(perturbations=[{"u": 0, "v": 0}] * 4)
    rec, _ = svc.create(p)
    assert rec["projection"]["whole_plane"] is True
    ans = svc.query("env-1", {"p": "100000000000000000000/7", "q": -99})
    assert ans["feasible"] is True


def test_canonical_fingerprint_includes_source_and_coefficients(services):
    _, svc = services
    spec_rec, _ = svc.create(env_payload())
    # 直接重算规格指纹核对：来源指纹 + 系数都进了指纹
    from app.envelope_models import parse_envelope_payload
    source_fp = spec_rec["source_fingerprint"]
    spec = parse_envelope_payload(env_payload(), 4)
    assert fingerprint(spec.canonical(source_fp)) == spec_rec["fingerprint"]


def test_concurrent_creates_single_frozen_record(services):
    """并发首次创建同规格包线：只允许一条冻结落盘，其余重放命中。"""
    import threading
    from pathlib import Path
    _, svc = services
    outcomes = []

    def worker():
        outcomes.append(
            svc.create(json.loads(json.dumps(env_payload())))
        )

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(outcomes) == 8
    firsts = sum(1 for _, replayed in outcomes if not replayed)
    replays = sum(1 for _, replayed in outcomes if replayed)
    assert firsts == 1 and replays == 7
    envelope_files = list(
        Path(services[0].data_dir, "envelopes").glob("*.json"))
    assert len(envelope_files) == 1


def test_envelope_and_audit_stores_are_separate(services, tmp_path):
    audit, svc = services
    svc.create(env_payload())
    # 审计目录下不应出现包线文件，反之亦然
    from pathlib import Path
    root = Path(audit.data_dir)
    audit_jsons = [p for p in root.glob("*.json")]
    envelope_jsons = list((root / "envelopes").glob("*.json"))
    assert len(audit_jsons) == 2 and len(envelope_jsons) == 1
