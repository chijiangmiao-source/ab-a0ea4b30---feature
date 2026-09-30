"""包线 HTTP 端到端：冻结/重放/冲突、来源准入、坐标查询、规模拒绝（真实套接字）。"""

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


FEAS = {
    "audit_id": "src-feas",
    "variables": ["x", "y"],
    "constraints": [
        {"coeffs": [-1, 0], "b": 0},
        {"coeffs": [0, -1], "b": 0},
        {"coeffs": [1, 0], "b": 0},
        {"coeffs": [0, 1], "b": 0},
        {"coeffs": [1, 1], "b": 1},
    ],
}
PERT = [[0, 0], [0, 0], [-1, 0], [0, -1], [1, 1]]
ENV = {"envelope_id": "env-http", "source_audit_id": "src-feas",
       "perturbations": PERT}


@pytest.fixture()
def with_source(server):
    assert req(server, "POST", "/api/audits", FEAS)[0] == 201
    return server


def test_index_mentions_envelope(server):
    with urllib.request.urlopen(server + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert "运行包线" in html and "Fourier" in html


def test_create_envelope_201_and_replay_200(with_source):
    status, body = req(with_source, "POST", "/api/envelopes", ENV)
    assert status == 201, body
    assert body["replayed"] is False
    rels = sorted(b["relation"] for b in body["result"]["boundaries"])
    assert rels == ["-u <= 0", "-v <= 0", "u + v <= 1"]
    assert body["result"]["bounded"] is True

    status, body2 = req(with_source, "POST", "/api/envelopes",
                        json.loads(json.dumps(ENV)))
    assert status == 200 and body2["replayed"] is True
    assert body2["fingerprint"] == body["fingerprint"]
    assert body2["created_at"] == body["created_at"]


def test_get_envelope_and_404(with_source):
    assert req(with_source, "GET", "/api/envelopes/ghost")[0] == 404
    req(with_source, "POST", "/api/envelopes", ENV)
    status, body = req(with_source, "GET", "/api/envelopes/env-http")
    assert status == 200 and body["envelope_id"] == "env-http"
    assert body["result"]["source_fingerprint"]


def test_unknown_source_400_no_artifact(with_source):
    bad = {**ENV, "source_audit_id": "missing"}
    status, body = req(with_source, "POST", "/api/envelopes", bad)
    assert status == 400 and body["error"] == "invalid_payload"
    assert "来源审计不存在" in body["detail"]
    assert req(with_source, "GET", "/api/envelopes/env-http")[0] == 404


def test_infeasible_source_400(server):
    inf = {"audit_id": "src-inf", "variables": ["x"],
           "constraints": [{"coeffs": [0], "b": -1}]}
    req(server, "POST", "/api/audits", inf)
    payload = {"envelope_id": "env-inf", "source_audit_id": "src-inf",
               "perturbations": [[0, 0]]}
    status, body = req(server, "POST", "/api/envelopes", payload)
    assert status == 400 and "不可行" in body["detail"]


def test_wrong_pair_count_400(with_source):
    bad = {**ENV, "perturbations": [[0, 0]]}
    status, body = req(with_source, "POST", "/api/envelopes", bad)
    assert status == 400 and "必须等于原约束条数" in body["detail"]
    assert req(with_source, "GET", "/api/envelopes/env-http")[0] == 404


def test_float_perturbation_400(with_source):
    bad = json.loads(json.dumps(ENV))
    bad["perturbations"][0] = [1.5, 0]
    status, body = req(with_source, "POST", "/api/envelopes", bad)
    assert status == 400 and body["error"] == "invalid_payload"


def test_changed_coefficients_409_and_unchanged(with_source):
    _, frozen = req(with_source, "POST", "/api/envelopes", ENV)
    changed = json.loads(json.dumps(ENV))
    changed["perturbations"][4] = [2, 2]
    status, body = req(with_source, "POST", "/api/envelopes", changed)
    assert status == 409 and body["error"] == "envelope_id_conflict"
    assert body["existing_fingerprint"] != body["new_fingerprint"]

    _, still = req(with_source, "GET", "/api/envelopes/env-http")
    assert still["fingerprint"] == frozen["fingerprint"]
    rels = sorted(b["relation"] for b in still["result"]["boundaries"])
    assert rels == ["-u <= 0", "-v <= 0", "u + v <= 1"]


def test_changed_source_same_envelope_id_409(server):
    req(server, "POST", "/api/audits", FEAS)
    other = {"audit_id": "src-other", "variables": ["x"],
             "constraints": [{"coeffs": [1], "b": 3},
                             {"coeffs": [-1], "b": 0}]}
    req(server, "POST", "/api/audits", other)
    req(server, "POST", "/api/envelopes", ENV)
    reuse = {"envelope_id": "env-http", "source_audit_id": "src-other",
             "perturbations": [[1, 0], [-1, 0]]}
    status, body = req(server, "POST", "/api/envelopes", reuse)
    assert status == 409, body


def test_query_inside_feasible_with_currents(with_source):
    req(with_source, "POST", "/api/envelopes", ENV)
    status, body = req(with_source, "POST",
                       "/api/envelopes/env-http/query",
                       {"u": "1/4", "v": "1/4"})
    assert status == 200 and body["status"] == "feasible"
    x = Fraction(body["currents"]["x"])
    y = Fraction(body["currents"]["y"])
    assert x >= 0 and y >= 0
    assert x + y + Fraction(1, 4) + Fraction(1, 4) <= 1
    assert all(not c["violated"] for c in body["boundary_checks"])


def test_query_boundary_point_feasible(with_source):
    req(with_source, "POST", "/api/envelopes", ENV)
    status, body = req(with_source, "POST",
                       "/api/envelopes/env-http/query",
                       {"u": "1", "v": "0"})
    assert status == 200 and body["status"] == "feasible"
    assert any(c["residual"] == "0" for c in body["boundary_checks"])


def test_query_outside_reports_first_violation(with_source):
    req(with_source, "POST", "/api/envelopes", ENV)
    status, body = req(with_source, "POST",
                       "/api/envelopes/env-http/query",
                       {"u": "1", "v": "1"})
    assert status == 200 and body["status"] == "infeasible"
    f = body["first_violation"]
    assert Fraction(f["gap"]) == 1
    assert " > " in f["substitution"]
    violated = [c["index"] for c in body["boundary_checks"] if c["violated"]]
    assert f["boundary_index"] == violated[0]


def test_query_unknown_envelope_404(with_source):
    status, body = req(with_source, "POST",
                       "/api/envelopes/ghost/query", {"u": 0, "v": 0})
    assert status == 404 and body["error"] == "not_found"


def test_query_bad_rational_400(with_source):
    req(with_source, "POST", "/api/envelopes", ENV)
    for bad in [{"u": "1.5", "v": 0}, {"u": "1/0", "v": 0},
                {"u": 0}, {"u": False, "v": 0}]:
        status, body = req(with_source, "POST",
                           "/api/envelopes/env-http/query", bad)
        assert status == 400, bad


def test_scale_exceeded_422(with_source):
    """确定性密集 8 变量系统使 FM 中间行膨胀超限：必须 422 拒绝且不落盘。"""
    import random
    rng = random.Random(0)  # 固定种子：该算例在消去第 2 个变量时即超 1024 行
    n, m = 8, 48
    A = [[rng.randint(-3, 3) for _ in range(n)] for _ in range(m)]
    b = [rng.randint(0, 50) for _ in range(m)]  # b>=0 保证 x=0 可行
    P = [rng.randint(-3, 3) for _ in range(m)]
    Q = [rng.randint(-3, 3) for _ in range(m)]
    dense = {
        "audit_id": "dense",
        "variables": [f"x{j}" for j in range(n)],
        "constraints": [
            {"coeffs": A[i], "b": b[i]} for i in range(m)
        ],
    }
    status, _ = req(with_source, "POST", "/api/audits", dense)
    assert status == 201
    payload = {
        "envelope_id": "env-dense",
        "source_audit_id": "dense",
        "perturbations": [[P[i], Q[i]] for i in range(m)],
    }
    status, body = req(with_source, "POST", "/api/envelopes", payload)
    assert status == 422, body
    assert body["error"] == "scale_exceeded"
    assert req(with_source, "GET", "/api/envelopes/env-dense")[0] == 404


def test_original_audit_flow_still_works(with_source):
    """包线能力上线后，原审计读取/精确余量/无解证书继续可用。"""
    status, body = req(with_source, "GET", "/api/audits/src-feas")
    assert status == 200 and body["result"]["status"] == "feasible"
    assert all(Fraction(m["residual"]) >= 0 for m in body["result"]["margins"])

    inf = {"audit_id": "still-inf", "variables": ["x"],
           "constraints": [{"coeffs": [0], "b": -1}]}
    status, body = req(with_source, "POST", "/api/audits", inf)
    assert status == 201 and body["result"]["combined_relation"] == "0 <= -1"
