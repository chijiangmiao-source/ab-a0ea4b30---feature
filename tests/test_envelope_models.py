"""包线请求与有理坐标的载荷校验测试。"""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.envelope_models import (
    parse_envelope_request,
    parse_point,
    parse_rational,
)
from app.models import PayloadError


def base_env(**over):
    p = {
        "envelope_id": "env-a",
        "source_audit_id": "audit-a",
        "perturbations": [[1, -2], [0, 3]],
    }
    p.update(over)
    return p


def test_env_valid():
    r = parse_envelope_request(base_env())
    assert r.envelope_id == "env-a"
    assert r.source_audit_id == "audit-a"
    assert r.perturbations == ((1, -2), (0, 3))


def test_env_canonical_includes_source_fingerprint():
    r = parse_envelope_request(base_env())
    c = r.canonical("sha256:abc")
    assert c["source_fingerprint"] == "sha256:abc"
    assert c["perturbations"] == [[1, -2], [0, 3]]
    # 改来源指纹必然改变规范载荷（复用派生标识改来源会被拒）
    assert r.canonical("sha256:xyz") != c


@pytest.mark.parametrize("bad", [
    None, [], "x",
    {"envelope_id": "", "source_audit_id": "a", "perturbations": [[0, 0]]},
    {"envelope_id": "a", "source_audit_id": "a", "perturbations": []},
    {"envelope_id": "a", "source_audit_id": "a", "perturbations": [[0]]},
    {"envelope_id": "a", "source_audit_id": "a", "perturbations": [(0, 0)]},
    {"envelope_id": "bad/id", "source_audit_id": "a", "perturbations": [[0, 0]]},
    {"envelope_id": "a", "source_audit_id": 9, "perturbations": [[0, 0]]},
])
def test_env_shape_rejected(bad):
    with pytest.raises(PayloadError):
        parse_envelope_request(bad)


@pytest.mark.parametrize("val", [1.0, True, False, "1", None, [1]])
def test_env_coeff_must_be_plain_int(val):
    p = base_env()
    p["perturbations"][0][0] = val
    with pytest.raises(PayloadError):
        parse_envelope_request(p)


def test_env_huge_int_rejected():
    p = base_env()
    p["perturbations"][0][0] = 10 ** 5000
    with pytest.raises(PayloadError, match="整数范围"):
        parse_envelope_request(p)


@pytest.mark.parametrize("v,expect", [
    (0, 0), (-3, -3), ("1/2", Fraction(1, 2)), ("-7/3", Fraction(-7, 3)),
    ("4", 4), ("+2", 2), ("2/-4", Fraction(-1, 2)),
])
def test_rational_accepted(v, expect):
    assert parse_rational(v, "x") == Fraction(expect)


@pytest.mark.parametrize("v", [1.5, True, "0.5", "1/0", "abc", "1/", None, [1]])
def test_rational_rejected(v):
    with pytest.raises(PayloadError):
        parse_rational(v, "x")


def test_parse_point_requires_both():
    assert parse_point({"u": "1/2", "v": -3}) == (Fraction(1, 2), Fraction(-3))
    with pytest.raises(PayloadError):
        parse_point({"u": 1})
    with pytest.raises(PayloadError):
        parse_point([1, 2])
