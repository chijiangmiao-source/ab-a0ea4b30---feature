"""verify 单次容器入口。

依次完成:
  1. 代码测试 (pytest)；
  2. HTTP 冒烟 (健康检查 / 页面 / 未知编号 404)；
  3. 用“相加得 0 <= -1”的约束组经真实 HTTP 提交核对不可行证书；
     独立重算 mu >= 0、Σmu*a = 0、Σmu*b < 0，并核对幂等(200)与冲突(409)。
  4. 再提交一个可行系统核对有理解与余量；
  5. 运行包线：页面含包线入口后，经真实 HTTP 创建冻结包线，独立重算每条
     规范化边界的来源合并式（Σλ·a=0、Σλ·(p,q)=(α,β)、Σλ·b=rhs、λ≥0），
     核对幂等与改动保护（409 且冻结不变）、来源/规模/非法有理拒绝
     （400/404/422），再对创建与有理坐标查询接口冒烟：
     内点给可复算电流（独立回代原约束），外点稳定标出首条违约边界与严格违反值。

任一步失败立即以非零退出码退出；全部成功退出 0。
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from fractions import Fraction

WEB_URL = os.environ.get("WEB_URL", "http://web:8080")
TIMEOUT_S = float(os.environ.get("VERIFY_TIMEOUT_S", "30"))


def step(title: str):
    print(f"\n=== verify: {title} ===", flush=True)


def http(method: str, path: str, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(WEB_URL + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def wait_healthy() -> None:
    step("等待 web 健康")
    deadline = time.time() + TIMEOUT_S
    last = None
    while time.time() < deadline:
        try:
            status, body = http("GET", "/healthz")
            if status == 200 and body.get("status") == "ok":
                print("healthz OK:", body)
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.5)
    raise SystemExit(f"web 在 {TIMEOUT_S}s 内未就绪: {last}")


def run_pytest() -> None:
    step("代码测试 pytest")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=os.environ.get("VERIFY_SRC_DIR", "/srv"),
    )
    if proc.returncode != 0:
        raise SystemExit(f"pytest 失败，退出码 {proc.returncode}")


def http_smoke() -> None:
    step("HTTP 冒烟")
    status, body = http("GET", "/healthz")
    assert status == 200 and body["status"] == "ok", body

    with urllib.request.urlopen(WEB_URL + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert resp.status == 200 and "单纯形" in html
    assert "运行包线" in html and "Fourier" in html, "页面必须含包线入口"

    status, _ = http("GET", "/api/audits/no-such-id-verify")
    assert status == 404, f"未知编号应为 404，实际 {status}"
    status, _ = http("GET", "/api/envelopes/no-such-id-verify")
    assert status == 404, f"未知包线编号应为 404，实际 {status}"
    print("健康检查 / 页面（含包线入口）/ 404 全部符合预期")


def check_certificate(audit_id: str, payload: dict, expect_rhs: str) -> dict:
    status, body = http("POST", "/api/audits", payload)
    # 对已持久化的命名卷重跑 verify 时可能命中 200 重放；显式重放另在下方核对
    assert status in (200, 201), (status, body)
    res = body["result"]
    assert res["status"] == "infeasible", res

    terms = res["terms"]
    n = len(payload["variables"])
    lhs = [Fraction(0) for _ in range(n)]
    rhs = Fraction(0)
    for t in terms:
        mu = Fraction(t["multiplier"])
        assert mu >= 0, "乘子必须非负"
        assert len(t["weighted_coeffs"]) == n
        assert Fraction(t["weighted_rhs"]) == mu * Fraction(t["b"])
        for j in range(n):
            lhs[j] += Fraction(t["weighted_coeffs"][j])
        rhs += Fraction(t["weighted_rhs"])
    assert all(v == 0 for v in lhs), f"左侧合并必须全为 0: {lhs}"
    assert rhs < 0, f"右侧合并必须严格为负: {rhs}"
    assert res["combined_rhs"] == expect_rhs == str(rhs)
    assert res["combined_relation"] == f"0 <= {expect_rhs}"
    print(f"[{audit_id}] 证书核验通过: mu>=0, Σmu·a=0, Σmu·b={rhs}")

    # 幂等：完全相同载荷重放 -> 200 同一记录
    status2, body2 = http("POST", "/api/audits",
                          json.loads(json.dumps(payload)))
    assert status2 == 200 and body2["replayed"] is True
    assert body2["fingerprint"] == body["fingerprint"]
    assert body2["created_at"] == body["created_at"]
    print(f"[{audit_id}] 重放返回同一冻结记录 (200)")

    # 冲突：同编号改载荷 -> 409，原证据不变
    changed = json.loads(json.dumps(payload))
    changed["constraints"][0]["b"] = changed["constraints"][0]["b"] + 1
    status3, body3 = http("POST", "/api/audits", changed)
    assert status3 == 409, (status3, body3)
    status4, body4 = http("GET", f"/api/audits/{audit_id}")
    assert status4 == 200 and body4["result"]["combined_rhs"] == expect_rhs
    print(f"[{audit_id}] 改动载荷冲突 409，原证据保持不变")
    return res


def main() -> int:
    run_pytest()
    wait_healthy()
    http_smoke()

    step("用相加得 0 <= -1 的约束组核对证书")
    # 组 1: 单条 0·x <= -1，乘子 1，直接合并为 0 <= -1
    check_certificate(
        "verify-zero-minus-one",
        {
            "audit_id": "verify-zero-minus-one",
            "variables": ["I1"],
            "constraints": [
                {"coeffs": [0], "b": -1, "stable": True}
            ],
        },
        "-1",
    )
    # 组 2: x <= 0 与 -x <= -1，乘子 1+1 合并为 0 <= -1
    check_certificate(
        "verify-combo",
        {
            "audit_id": "verify-combo",
            "variables": ["I1", "I2"],
            "constraints": [
                {"coeffs": [1, 0], "b": 0, "stable": False},
                {"coeffs": [-1, 0], "b": -1, "stable": True},
            ],
        },
        "-1",
    )

    step("可行系统冒烟: 1 <= x <= 2")
    p = {
        "audit_id": "verify-feasible",
        "variables": ["I1"],
        "constraints": [
            {"coeffs": [1], "b": 2},
            {"coeffs": [-1], "b": -1},
        ],
    }
    status, body = http("POST", "/api/audits", p)
    assert status in (200, 201) and body["result"]["status"] == "feasible"
    x = Fraction(body["result"]["currents"]["I1"])
    assert 1 <= x <= 2
    for m in body["result"]["margins"]:
        assert Fraction(m["residual"]) >= 0
    print("可行解与精确余量核验通过")

    check_envelope()

    print("\nVERIFY OK: 代码测试、镜像运行与 HTTP 冒烟、精确证书与运行包线全部通过",
          flush=True)
    return 0


# ------------------------- 运行包线核对 -------------------------

# 与 tests 中有界三角形同构（经真实 HTTP 重新构造）：
# x>=0,y>=0,x-u<=0,y-v<=0,x+y+u+v<=1  => u>=0, v>=0, u+v<=1
ENV_AUDIT = {
    "audit_id": "verify-env-src",
    "variables": ["x", "y"],
    "constraints": [
        {"coeffs": [-1, 0], "b": 0, "stable": True},
        {"coeffs": [0, -1], "b": 0, "stable": True},
        {"coeffs": [1, 0], "b": 0},
        {"coeffs": [0, 1], "b": 0},
        {"coeffs": [1, 1], "b": 1},
    ],
}
ENV_PERT = [[0, 0], [0, 0], [-1, 0], [0, -1], [1, 1]]
ENV_REQ = {
    "envelope_id": "verify-env",
    "source_audit_id": "verify-env-src",
    "perturbations": ENV_PERT,
}


def check_envelope() -> None:
    step("运行包线：冻结、边界证据、拒绝保护、坐标查询")

    # 先冻结一个可行来源审计（幂等重放也允许命中已有记录）
    status, _ = http("POST", "/api/audits", ENV_AUDIT)
    assert status in (200, 201), "来源可行审计提交失败"

    # 1) 首次冻结（对已存在的持久化卷重跑 verify 时可能命中 200 重放）
    status, body = http("POST", "/api/envelopes", ENV_REQ)
    assert status in (200, 201), (status, body)
    res = body["result"]
    assert res["status"] == "envelope" and res["bounded"] is True
    bds = res["boundaries"]
    rels = sorted(b["relation"] for b in bds)
    assert rels == ["-u <= 0", "-v <= 0", "u + v <= 1"], rels

    # 2) 独立重算每条边界的来源合并式
    A = [c["coeffs"] for c in ENV_AUDIT["constraints"]]
    b = [c["b"] for c in ENV_AUDIT["constraints"]]
    P = [pq[0] for pq in ENV_PERT]
    Q = [pq[1] for pq in ENV_PERT]
    m, n = len(b), len(ENV_AUDIT["variables"])
    for bd in bds:
        lam = [Fraction(0)] * m
        for t in bd["sources"]:
            mu = Fraction(t["multiplier"])
            assert mu > 0, "来源乘子必须严格为正"
            lam[t["index"]] = mu
            # 逐项自洽
            assert [Fraction(s) for s in t["weighted_coeffs"]] == \
                [mu * Fraction(v) for v in A[t["index"]]]
            assert Fraction(t["weighted_p"]) == mu * P[t["index"]]
            assert Fraction(t["weighted_q"]) == mu * Q[t["index"]]
            assert Fraction(t["weighted_rhs"]) == mu * b[t["index"]]
        for j in range(n):
            assert sum(lam[i] * A[i][j] for i in range(m)) == 0, \
                f"边界 {bd['index']} 电流列 {j} 未消净"
        assert sum(lam[i] * P[i] for i in range(m)) == int(bd["alpha"])
        assert sum(lam[i] * Q[i] for i in range(m)) == int(bd["beta"])
        assert sum(lam[i] * b[i] for i in range(m)) == int(bd["rhs"])
    print("边界证据核验通过：λ≥0、Σλ·a=0、Σλ·(p,q)=(α,β)、Σλ·b=rhs")

    # 3) 幂等重放：同载荷 -> 200 同一冻结记录
    status, body2 = http("POST", "/api/envelopes",
                         json.loads(json.dumps(ENV_REQ)))
    assert status == 200 and body2["replayed"] is True
    assert body2["fingerprint"] == body["fingerprint"]
    assert body2["created_at"] == body["created_at"]

    # 4) 改动扰动系数 -> 409，冻结包线不变
    changed = json.loads(json.dumps(ENV_REQ))
    changed["perturbations"][4] = [2, 2]
    status, conflict = http("POST", "/api/envelopes", changed)
    assert status == 409, (status, conflict)
    assert conflict["existing_fingerprint"] == body["fingerprint"]
    status, still = http("GET", "/api/envelopes/verify-env")
    assert status == 200
    assert sorted(x["relation"] for x in still["result"]["boundaries"]) == rels

    # 复用派生标识却改来源 -> 409
    other_audit = {
        "audit_id": "verify-env-src-other",
        "variables": ["x"],
        "constraints": [{"coeffs": [1], "b": 3}, {"coeffs": [-1], "b": 0}],
    }
    http("POST", "/api/audits", other_audit)
    reuse = {"envelope_id": "verify-env", "source_audit_id": "verify-env-src-other",
             "perturbations": [[1, 0], [-1, 0]]}
    status, _ = http("POST", "/api/envelopes", reuse)
    assert status == 409

    # 5) 拒绝保护：来源不存在 400、来源不可行 400、对数不符 400、
    #    非整数扰动 400、查询非法有理 400、查询未知包线 404
    status, err = http("POST", "/api/envelopes",
                       {**ENV_REQ, "envelope_id": "verify-env-missing",
                        "source_audit_id": "no-such-audit"})
    assert status == 400 and "来源审计不存在" in err["detail"]

    inf = {"audit_id": "verify-env-inf", "variables": ["x"],
           "constraints": [{"coeffs": [0], "b": -1}]}
    http("POST", "/api/audits", inf)
    status, err = http("POST", "/api/envelopes",
                       {"envelope_id": "verify-env-inf-e",
                        "source_audit_id": "verify-env-inf",
                        "perturbations": [[0, 0]]})
    assert status == 400 and "不可行" in err["detail"]

    status, err = http("POST", "/api/envelopes",
                       {**ENV_REQ, "envelope_id": "verify-env-badcount",
                        "perturbations": [[0, 0]]})
    assert status == 400 and "必须等于原约束条数" in err["detail"]

    bad_int = json.loads(json.dumps(ENV_REQ))
    bad_int["envelope_id"] = "verify-env-float"
    bad_int["perturbations"][0] = [1.5, 0]
    status, err = http("POST", "/api/envelopes", bad_int)
    assert status == 400 and err["error"] == "invalid_payload"

    status, err = http("POST", "/api/envelopes/verify-env/query",
                       {"u": "1/0", "v": 0})
    assert status == 400
    status, err = http("POST", "/api/envelopes/no-such-env/query",
                       {"u": 0, "v": 0})
    assert status == 404

    # 6) 超过受理规模 -> 422，且不落盘（固定种子密集 8 变量算例）
    rng = random.Random(0)
    n8, m8 = 8, 48
    dA = [[rng.randint(-3, 3) for _ in range(n8)] for _ in range(m8)]
    db = [rng.randint(0, 50) for _ in range(m8)]
    dP = [rng.randint(-3, 3) for _ in range(m8)]
    dQ = [rng.randint(-3, 3) for _ in range(m8)]
    dense = {"audit_id": "verify-env-dense-src",
             "variables": [f"x{j}" for j in range(n8)],
             "constraints": [{"coeffs": dA[i], "b": db[i]} for i in range(m8)]}
    status, _ = http("POST", "/api/audits", dense)
    assert status in (200, 201)
    status, err = http("POST", "/api/envelopes", {
        "envelope_id": "verify-env-dense",
        "source_audit_id": "verify-env-dense-src",
        "perturbations": [[dP[i], dQ[i]] for i in range(m8)],
    })
    assert status == 422 and err["error"] == "scale_exceeded", (status, err)
    assert http("GET", "/api/envelopes/verify-env-dense")[0] == 404
    print("拒绝保护核验通过：不存在/不可行/规模/非法载荷均被拒绝且不落盘")

    # 7) 创建与坐标查询接口冒烟
    # 内点 (1/4, 1/4)：可行，给一组可复算电流，独立回代原扰动约束
    status, ans = http("POST", "/api/envelopes/verify-env/query",
                       {"u": "1/4", "v": "1/4"})
    assert status == 200 and ans["status"] == "feasible", ans
    u, v = Fraction(1, 4), Fraction(1, 4)
    cur = {k: Fraction(z) for k, z in ans["currents"].items()}
    assert all(not c["violated"] for c in ans["boundary_checks"])
    for i in range(m):
        lhs = A[i][0] * cur["x"] + A[i][1] * cur["y"] + P[i] * u + Q[i] * v
        assert lhs <= b[i], f"内点回代原约束 {i} 被违反"
    # 顶点 (1,0)：恰落在 u+v<=1 上，余量为 0，仍判可行
    status, corner = http("POST", "/api/envelopes/verify-env/query",
                          {"u": "1", "v": "0"})
    assert status == 200 and corner["status"] == "feasible"
    assert any(c["residual"] == "0" for c in corner["boundary_checks"])
    # 外点 (1,1)：稳定标出首条违约边界与严格违反值
    status, out = http("POST", "/api/envelopes/verify-env/query",
                       {"u": "1", "v": "1"})
    assert status == 200 and out["status"] == "infeasible"
    f = out["first_violation"]
    assert Fraction(f["gap"]) == 1 and Fraction(f["lhs"]) == 2
    viol = [c["index"] for c in out["boundary_checks"] if c["violated"]]
    assert f["boundary_index"] == viol[0], "首条违约边界须稳定"
    print("坐标查询冒烟通过：内点可复算电流、边界点余量 0、外点首违与严格违反值正确")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"VERIFY FAILED: {exc}", flush=True)
        sys.exit(1)
