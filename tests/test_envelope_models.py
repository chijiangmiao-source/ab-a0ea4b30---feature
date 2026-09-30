"""包线载荷与有理坐标解析的整数级校验测试。"""

from __future__ import annotations

import pytest

from app.envelope_models import (
    EnvelopePayloadError,
    parse_envelope_header,
    parse_envelope_payload,
    parse_perturbations,
    parse_rational,
)
from fractions import Fraction


def payload(**over):
    p = {
        "envelope_id": "env-1",
        "source_audit_id": "audit-1",
        "perturbations": [
            {"u": 1, "v": -2},
            {"u": 0, "v": 0},
        ],
    }
    p.update(over)
    return p


def test_valid_spec():
    spec = parse_envelope_payload(payload(), 2)
    assert spec.envelope_id == "env-1"
    assert spec.source_audit_id == "audit-1"
    assert spec.us == (1, 0) and spec.vs == (-2, 0)


def test_header_and_perturbations_separable():
    eid, sid = parse_envelope_header(payload())
    assert (eid, sid) == ("env-1", "audit-1")
    us, vs = parse_perturbations(payload(), 2)
    assert us == (1, 0) and vs == (-2, 0)


def test_not_object():
    with pytest.raises(EnvelopePayloadError):
        parse_envelope_header([1, 2])
    with pytest.raises(EnvelopePayloadError):
        parse_envelope_header("x")


@pytest.mark.parametrize("field", ["envelope_id", "source_audit_id"])
def test_bad_ids(field):
    for bad in ["", "   ", "a b", "a/b", "x" * 200, None, 7]:
        with pytest.raises(EnvelopePayloadError):
            parse_envelope_header(payload(**{field: bad}))


def test_perturbation_count_mismatch():
    with pytest.raises(EnvelopePayloadError, match="严格相等"):
        parse_perturbations(payload(), 3)
    with pytest.raises(EnvelopePayloadError, match="严格相等"):
        parse_perturbations(payload(), 1)


def test_empty_perturbations():
    with pytest.raises(EnvelopePayloadError):
        parse_perturbations(payload(perturbations=[]), 0)


def test_perturbation_not_object():
    p = payload(perturbations=[1, 2])
    with pytest.raises(EnvelopePayloadError):
        parse_perturbations(p, 2)


@pytest.mark.parametrize("bad", [1.0, True, "3", None, 1.5])
def test_u_must_be_plain_int(bad):
    p = payload(perturbations=[{"u": bad, "v": 0}, {"u": 0, "v": 0}])
    with pytest.raises(EnvelopePayloadError):
        parse_perturbations(p, 2)


@pytest.mark.parametrize("bad", [2.0, False, "-1", None])
def test_v_must_be_plain_int(bad):
    p = payload(perturbations=[{"u": 0, "v": 1}, {"u": 0, "v": bad}])
    with pytest.raises(EnvelopePayloadError):
        parse_perturbations(p, 2)


def test_big_int_accepted():
    p = payload(perturbations=[{"u": 10**100, "v": -10**100},
                               {"u": 0, "v": 0}])
    spec = parse_envelope_payload(p, 2)
    assert spec.us[0] == 10**100 and spec.vs[0] == -10**100


def test_too_big_int_rejected():
    p = payload(perturbations=[{"u": 2 ** 5000, "v": 0},
                               {"u": 0, "v": 0}])
    with pytest.raises(EnvelopePayloadError):
        parse_perturbations(p, 2)


def test_canonical_binds_source_fingerprint():
    spec = parse_envelope_payload(payload(), 2)
    c1 = spec.canonical("fp-A")
    c2 = spec.canonical("fp-B")
    assert c1["source_fingerprint"] == "fp-A"
    assert c1 != c2  # 换来源（指纹变化）必导致幂等指纹不同


# ---------------- 有理坐标 ----------------

@pytest.mark.parametrize("value,expected", [
    (0, Fraction(0)), (5, Fraction(5)), (-7, Fraction(-7)),
    ("1", Fraction(1)), ("-1/2", Fraction(-1, 2)),
    (" 3 / 4 ", Fraction(3, 4)), ("1.25", Fraction(5, 4)),
    ("-.5", Fraction(-1, 2)), ("100000000000000000000/3",
                                Fraction(10**20, 3)),
])
def test_rational_accepted(value, expected):
    assert parse_rational(value, "p") == expected


@pytest.mark.parametrize("bad", [
    1.5, -1.25, True, False, None, [1],
    "", "abc", "1/2/3", "1e3", "NaN", "Infinity", "1/0",
])
def test_rational_rejected(bad):
    with pytest.raises(EnvelopePayloadError):
        parse_rational(bad, "p")


def test_rational_denominator_zero():
    with pytest.raises(EnvelopePayloadError, match="分母"):
        parse_rational("5/0", "q")


def test_rational_width_limit():
    with pytest.raises(EnvelopePayloadError):
        parse_rational(f"1/{2 ** 5000}", "p")
