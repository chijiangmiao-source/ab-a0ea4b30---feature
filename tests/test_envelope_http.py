"""包线 HTTP 端到端：真实套接字上的创建/重放/冲突/拒绝/坐标查询语义。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from fractions import Fraction

import pytest

from app.web import make_server


@pytest.fixture()
def server(tmp_path):
    httpd = make_server(host="127.0.0.1", port=0,
                        data_dir=str(tmp_path / "data"))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    port = httpd.server_address[1]
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def req(base, method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, data=data, headers=headers,
                               method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


FEAS_AUDIT = {
    "audit_id": "trap-1",
    "variables": ["I1", "I2"],
    "constraints": [
        {"coeffs": [1, 0], "b": 2, "label": "I1-up", "stable": True},
        {"coeffs": [0, 1], "b": 3, "label": "I2-up", "stable": False},
        {"coeffs": [-1, 0], "b": 0, "stable": True},
        {"coeffs": [0, -1], "b": 0, "stable": True},
    ],
}

INF_AUDIT = {
    "audit_id": "trap-inf",
    "variables": ["I1"],
    "constraints": [
        {"coeffs": [1], "b": 0},
        {"coeffs": [-1], "b": -1},
    ],
}


def make_env(eid="env-1", sid="trap-1", pert=None):
    if pert is None:
        pert = [
            {"u": 1, "v": 0}, {"u": 0, "v": 1},
            {"u": 0, "v": 0}, {"u": 0, "v": 0},
        ]
    return {"envelope_id": eid, "source_audit_id": sid,
            "perturbations": pert}


@pytest.fixture()
def ready(server):
    s, _ = req(server, "POST", "/api/audits", FEAS_AUDIT)
    assert s == 201
    s, _ = req(server, "POST", "/api/audits", INF_AUDIT)
    assert s == 201
    return server


def test_create_envelope_201_and_halfplanes(ready):
    s, b = req(ready, "POST", "/api/envelopes", make_env())
    assert s == 201 and b["replayed"] is False
    keys = [(h["alpha"], h["beta"], h["gamma"])
            for h in b["projection"]["halfplanes"]]
    assert keys == [(-1, 0, 2), (0, -1, 3)]
    # 页面与来源信息都在
    assert b["source_fingerprint"].startswith("sha256:")
    assert b["variables"] == ["I1", "I2"]


def test_replay_envelope_200(ready):
    req(ready, "POST", "/api/envelopes", make_env())
    s, b = req(ready, "POST", "/api/envelopes",
               json.loads(json.dumps(make_env())))
    assert s == 200 and b["replayed"] is True


def test_get_envelope_404_and_200(ready):
    assert req(ready, "GET", "/api/envelopes/ghost")[0] == 404
    req(ready, "POST", "/api/envelopes", make_env())
    s, b = req(ready, "GET", "/api/envelopes/env-1")
    assert s == 200 and b["envelope_id"] == "env-1"


def test_change_coefficients_409_preserves(ready):
    s0, b0 = req(ready, "POST", "/api/envelopes", make_env())
    assert s0 == 201
    changed = make_env()
    changed["perturbations"][1]["v"] = 4
    s, b = req(ready, "POST", "/api/envelopes", changed)
    assert s == 409 and b["error"] == "envelope_id_conflict"
    assert b["existing_fingerprint"] != b["new_fingerprint"]
    s, b = req(ready, "GET", "/api/envelopes/env-1")
    keys = [(h["alpha"], h["beta"], h["gamma"])
            for h in b["projection"]["halfplanes"]]
    assert keys == [(-1, 0, 2), (0, -1, 3)]


def test_change_source_409(ready):
    # 建一条不同内容但同变量数/约束数的来源
    other = {
        "audit_id": "trap-2",
        "variables": ["I1", "I2"],
        "constraints": [
            {"coeffs": [1, 0], "b": 20},
            {"coeffs": [0, 1], "b": 30},
            {"coeffs": [-1, 0], "b": 0},
            {"coeffs": [0, -1], "b": 0},
        ],
    }
    assert req(ready, "POST", "/api/audits", other)[0] == 201
    req(ready, "POST", "/api/envelopes", make_env(sid="trap-1"))
    s, b = req(ready, "POST", "/api/envelopes",
               make_env(sid="trap-2"))
    assert s == 409 and b["error"] == "envelope_id_conflict"
    s, b = req(ready, "GET", "/api/envelopes/env-1")
    assert b["source_audit_id"] == "trap-1"


def test_source_missing_404(ready):
    s, b = req(ready, "POST", "/api/envelopes",
               make_env(sid="missing"))
    assert s == 404 and b["error"] == "source_not_found"


def test_infeasible_source_422(ready):
    s, b = req(ready, "POST", "/api/envelopes",
               make_env(sid="trap-inf",
                        pert=[{"u": 0, "v": 0}, {"u": 0, "v": 0}]))
    assert s == 422 and b["error"] == "source_infeasible"


def test_invalid_perturbations_400(ready):
    bad = make_env(pert=[{"u": 1, "v": 0.5}] * 4)
    s, b = req(ready, "POST", "/api/envelopes", bad)
    assert s == 400 and b["error"] == "invalid_payload"
    # 数量不符
    bad2 = make_env(pert=[{"u": 0, "v": 0}])
    s, b = req(ready, "POST", "/api/envelopes", bad2)
    assert s == 400
    # 非法 envelope_id
    bad3 = make_env(eid="bad id")
    s, b = req(ready, "POST", "/api/envelopes", bad3)
    assert s == 400


def test_rejected_envelope_does_not_touch_audit(ready):
    fp_before = req(ready, "GET", "/api/audits/trap-1")[1]["fingerprint"]
    req(ready, "POST", "/api/envelopes", make_env(sid="trap-inf",
        pert=[{"u": 0, "v": 0}, {"u": 0, "v": 0}]))
    req(ready, "POST", "/api/envelopes", make_env(sid="missing"))
    fp_after = req(ready, "GET", "/api/audits/trap-1")[1]["fingerprint"]
    assert fp_before == fp_after


def test_query_inside_returns_recheckable_currents(ready):
    req(ready, "POST", "/api/envelopes", make_env())
    s, b = req(ready, "POST", "/api/envelopes/env-1/query",
               {"p": "-1/2", "q": "1/3"})
    assert s == 200 and b["feasible"] is True
    cur = {k: Fraction(v) for k, v in b["currents"].items()}
    A = [c["coeffs"] for c in FEAS_AUDIT["constraints"]]
    # u*p 作用于约束 0（u=1,p=-1/2）；v*q 作用于约束 1（v=1,q=1/3）
    perturb = [-Fraction(1, 2), Fraction(1, 3), 0, 0]
    for i, con in enumerate(FEAS_AUDIT["constraints"]):
        rhs = Fraction(con["b"]) + perturb[i]
        lhs = sum(A[i][j] * cur[k] for j, k in enumerate(cur))
        assert lhs <= rhs
        m = b["margins"][i]
        assert Fraction(m["residual"]) == rhs - lhs


def test_query_outside_first_boundary_and_strict_excess(ready):
    req(ready, "POST", "/api/envelopes", make_env())
    s, b = req(ready, "POST", "/api/envelopes/env-1/query",
               {"p": -3, "q": -7})
    assert s == 200 and b["feasible"] is False
    v = b["violation"]
    # p 与 q 同时越界，必须稳定给规范顺序首条 (-p<=2, index 0)
    assert v["index"] == 0
    assert v["line"] == "-p <= 2"
    assert v["substitution"] == "3"
    assert v["strict_excess"] == "1"
    assert all(x == "0" for x in v["derivation"]["combined_coeffs"])


def test_query_boundary_equality_feasible(ready):
    req(ready, "POST", "/api/envelopes", make_env())
    s, b = req(ready, "POST", "/api/envelopes/env-1/query",
               {"p": "-2", "q": "-3"})
    assert s == 200 and b["feasible"] is True


def test_query_invalid_coordinate_400(ready):
    req(ready, "POST", "/api/envelopes", make_env())
    for body in [{"p": 0.5, "q": 0}, {"p": "nope", "q": 0},
                 {"p": "1/0", "q": 0}, {"p": 0}]:
        s, b = req(ready, "POST", "/api/envelopes/env-1/query", body)
        assert s == 400 and b["error"] == "invalid_coordinate", body


def test_query_unknown_envelope_404(ready):
    s, b = req(ready, "POST", "/api/envelopes/nope/query",
               {"p": 0, "q": 0})
    assert s == 404 and b["error"] == "envelope_not_found"


def test_whole_plane_envelope_http(ready):
    s, b = req(ready, "POST", "/api/envelopes",
               make_env(eid="env-w", pert=[{"u": 0, "v": 0}] * 4))
    assert s == 201
    assert b["projection"]["whole_plane"] is True
    s, b = req(ready, "POST", "/api/envelopes/env-w/query",
               {"p": 123456789, "q": "-987654321/13"})
    assert s == 200 and b["feasible"] is True


def test_index_page_contains_envelope_ui(ready):
    with urllib.request.urlopen(ready + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert resp.status == 200
    assert "Fourier" in html and "包线" in html


def test_envelope_persists_across_server_instances(tmp_path):
    data = str(tmp_path / "data")

    def start():
        import app.web as web
        web.Handler.service = None
        web.Handler.envelope_service = None
        httpd = make_server("127.0.0.1", 0, data)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd

    s1 = start()
    base1 = f"http://127.0.0.1:{s1.server_address[1]}"
    req(base1, "POST", "/api/audits", FEAS_AUDIT)
    req(base1, "POST", "/api/envelopes", make_env())
    s1.shutdown()
    s1.server_close()

    s2 = start()
    base2 = f"http://127.0.0.1:{s2.server_address[1]}"
    s, b = req(base2, "GET", "/api/envelopes/env-1")
    assert s == 200
    keys = [(h["alpha"], h["beta"], h["gamma"])
            for h in b["projection"]["halfplanes"]]
    assert keys == [(-1, 0, 2), (0, -1, 3)]
    s, b = req(base2, "POST", "/api/envelopes/env-1/query",
               {"p": 0, "q": 0})
    assert s == 200 and b["feasible"] is True
    s2.shutdown()
    s2.server_close()
