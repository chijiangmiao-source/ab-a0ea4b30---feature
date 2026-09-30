"""verify 单次容器入口。

依次完成:
  1. 代码测试 (pytest)；
  2. HTTP 冒烟 (健康检查 / 页面 / 未知编号 404)；
  3. 用“相加得 0 <= -1”的约束组经真实 HTTP 提交核对不可行证书；
     独立重算 mu >= 0、Σmu*a = 0、Σmu*b < 0，并核对幂等(200)与冲突(409)。
  4. 运行包线：经真实 HTTP 创建冻结包线，独立复算每条规范半平面的
     来源合并式（λ>=0、Σλ·a=0、与规范行严格正比例）；查询包线内
     有理坐标（独立回算电流与余量）与包线外坐标（首条违约边界、
     严格违反值、代入结果）；并核对来源缺失(404)/不可行(422)/
     复用编号改系数或改来源(409, 原冻结不变)/非法载荷(400) 等拒绝保护。

任一步失败立即以非零退出码退出；全部成功退出 0。
"""

from __future__ import annotations

import json
import math
import os
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

    status, _ = http("GET", "/api/audits/no-such-id-verify")
    assert status == 404, f"未知编号应为 404，实际 {status}"
    print("健康检查 / 页面 / 404 全部符合预期")


def check_certificate(audit_id: str, payload: dict, expect_rhs: str) -> dict:
    status, body = http("POST", "/api/audits", payload)
    assert status == 201, (status, body)
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
    assert status == 201 and body["result"]["status"] == "feasible"
    x = Fraction(body["result"]["currents"]["I1"])
    assert 1 <= x <= 2
    for m in body["result"]["margins"]:
        assert Fraction(m["residual"]) >= 0
    print("可行解与精确余量核验通过")

    verify_envelopes()

    print("\nVERIFY OK: 代码测试、镜像运行与 HTTP 冒烟、精确证书全部通过", flush=True)
    return 0


# ---------------------------------------------------------------- 包线验收

def _fm_feasible(rows):
    """独立于 app 的极简有理数 FM 可行性判定（自由变量，只用于验收复算）。

    rows: (系数 list[Fraction], 右端 Fraction)，判定是否存在自由 x。
    """
    rows = [([Fraction(c) for c in v], Fraction(r)) for v, r in rows]
    while rows and rows[0][0]:
        pos, neg, zero = [], [], []
        for v, r in rows:
            c = v[0]
            (pos if c > 0 else neg if c < 0 else zero).append((v, r))
        new = [(v[1:], r) for v, r in zero]
        for vp, rp in pos:
            for vn, rn in neg:
                f1, f2 = 1 / vp[0], -1 / vn[0]
                new.append(([f1 * vp[k] + f2 * vn[k]
                             for k in range(1, len(vp))],
                            f1 * rp + f2 * rn))
        rows = new
    return all(r >= 0 for _, r in rows)


def verify_envelopes() -> None:
    step("运行包线：冻结、来源合并式、坐标查询与拒绝保护")

    # 来源：0<=I1<=2, 0<=I2<=3, I1+I2<=4（可行审计）
    audit = {
        "audit_id": "verify-env-audit",
        "variables": ["I1", "I2"],
        "constraints": [
            {"coeffs": [1, 0], "b": 2, "label": "I1-up", "stable": True},
            {"coeffs": [0, 1], "b": 3, "label": "I2-up", "stable": False},
            {"coeffs": [-1, 0], "b": 0, "stable": True},
            {"coeffs": [0, -1], "b": 0, "stable": True},
            {"coeffs": [1, 1], "b": 4, "stable": False},
        ],
    }
    status, _ = http("POST", "/api/audits", audit)
    assert status == 201

    env_payload = {
        "envelope_id": "verify-env",
        "source_audit_id": "verify-env-audit",
        # 上限随 p/q 放宽，其余固定；期望投影 -p<=2 与 -q<=3
        "perturbations": [
            {"u": 1, "v": 0}, {"u": 0, "v": 1},
            {"u": 0, "v": 0}, {"u": 0, "v": 0}, {"u": 0, "v": 0},
        ],
    }
    status, body = http("POST", "/api/envelopes", env_payload)
    assert status == 201, (status, body)
    proj = body["projection"]
    assert proj["method"] == "exact-rational/fourier-motzkin"
    hps = proj["halfplanes"]
    keys = [(h["alpha"], h["beta"], h["gamma"]) for h in hps]
    assert keys == [(-1, 0, 2), (0, -1, 3)], keys
    # 无重复表示，且每条本原
    assert len(keys) == len(set(keys))
    for a, b, g in keys:
        assert math.gcd(math.gcd(abs(a), abs(b)), abs(g)) == 1

    # 独立复算每条半平面的来源合并式
    srcs = body["sources"]
    n = len(audit["variables"])
    for h in hps:
        cur = [Fraction(0) for _ in range(n)]
        pc = qc = rc = Fraction(0)
        for term in h["derivation"]["terms"]:
            i, lam = term["index"], Fraction(term["multiplier"])
            assert lam > 0
            for j in range(n):
                cur[j] += lam * Fraction(term["weighted_coeffs"][j])
            pc += Fraction(term["weighted_p"])
            qc += Fraction(term["weighted_q"])
            rc += Fraction(term["weighted_rhs"])
            s = srcs[i]
            assert Fraction(term["weighted_rhs"]) == lam * s["b"]
            assert Fraction(term["weighted_p"]) == -lam * s["u"]
            assert Fraction(term["weighted_q"]) == -lam * s["v"]
        assert all(c == 0 for c in cur), f"电流系数必须消净: {cur}"
        a, b, g = h["alpha"], h["beta"], h["gamma"]
        t = pc / a if a != 0 else qc / b
        assert t > 0 and pc == a * t and qc == b * t and rc == g * t
    print("包线冻结与来源合并式独立复算通过:",
          ", ".join(h["line"] for h in hps))

    # 幂等重放
    status, body2 = http("POST", "/api/envelopes",
                         json.loads(json.dumps(env_payload)))
    assert status == 200 and body2["replayed"] is True
    assert body2["fingerprint"] == body["fingerprint"]
    assert body2["created_at"] == body["created_at"]

    # ---- 坐标查询：内点给可复算电流，外点给首条违约边界 ----
    def concrete_rows(p, q):
        return [
            (s["coeffs"], Fraction(s["b"]) + s["u"] * p + s["v"] * q)
            for s in srcs
        ]

    inside_pts = [(-2, -3), (Fraction(-1, 2), Fraction(1, 3)), (0, 0)]
    outside_pts = [(-3, 0), (0, -4), (-Fraction(5, 2), -Fraction(7, 2))]
    for p, q in inside_pts:
        status, ans = http("POST", "/api/envelopes/verify-env/query",
                           {"p": str(p), "q": str(q)})
        assert status == 200 and ans["feasible"] is True, (p, q, ans)
        # 与独立 FM 判定一致
        assert _fm_feasible(concrete_rows(p, q)) is True
        cur = {k: Fraction(v) for k, v in ans["currents"].items()}
        for s in srcs:
            rhs = Fraction(s["b"]) + s["u"] * p + s["v"] * q
            lhs = sum(Fraction(s["coeffs"][j]) * cur[body["variables"][j]]
                      for j in range(n))
            assert lhs <= rhs, (s, lhs, rhs)
    for p, q in outside_pts:
        assert _fm_feasible(concrete_rows(p, q)) is False  # 独立确认确在包线外
        status, ans = http("POST", "/api/envelopes/verify-env/query",
                           {"p": str(p), "q": str(q)})
        assert status == 200 and ans["feasible"] is False
        v = ans["violation"]
        lhs = Fraction(v["alpha"]) * p + Fraction(v["beta"]) * q
        assert Fraction(v["substitution"]) == lhs
        excess = lhs - v["gamma"]
        assert Fraction(v["strict_excess"]) == excess and excess > 0
        # 首条：规范顺序中没有更早的被违反边界
        for earlier in hps[:v["index"]]:
            assert (Fraction(earlier["alpha"]) * p
                    + Fraction(earlier["beta"]) * q) <= earlier["gamma"]
    print("包线内电流回算与包线外首违边界（含严格违反值）核验通过")

    # ---- 拒绝保护 ----
    # 来源不存在 -> 404
    missing = dict(env_payload, envelope_id="verify-env-missing",
                   source_audit_id="no-such-audit")
    status, b = http("POST", "/api/envelopes", missing)
    assert status == 404 and b["error"] == "source_not_found"

    # 非法载荷（浮点系数 / 数量不符）-> 400
    bad = json.loads(json.dumps(env_payload))
    bad["envelope_id"] = "verify-env-bad"
    bad["perturbations"][0]["u"] = 1.5
    status, b = http("POST", "/api/envelopes", bad)
    assert status == 400 and b["error"] == "invalid_payload"
    bad2 = dict(env_payload, envelope_id="verify-env-bad2",
                perturbations=[{"u": 0, "v": 0}])
    assert http("POST", "/api/envelopes", bad2)[0] == 400

    # 不可行来源 -> 422
    inf_audit = {
        "audit_id": "verify-env-infsrc",
        "variables": ["I1"],
        "constraints": [{"coeffs": [1], "b": 0},
                        {"coeffs": [-1], "b": -1}],
    }
    assert http("POST", "/api/audits", inf_audit)[0] == 201
    inf_env = {
        "envelope_id": "verify-env-inf",
        "source_audit_id": "verify-env-infsrc",
        "perturbations": [{"u": 0, "v": 0}, {"u": 0, "v": 0}],
    }
    status, b = http("POST", "/api/envelopes", inf_env)
    assert status == 422 and b["error"] == "source_infeasible"

    # 超过受理规模 -> 422 envelope_too_large（确定性稠密构造，必须精确拒绝而非近似）
    n8 = 8
    dense_cons = []
    for j in range(n8):
        dense_cons.append({
            "coeffs": [1 if k == j else 0 for k in range(n8)],
            "b": 100,
        })
        dense_cons.append({
            "coeffs": [-1 if k == j else 0 for k in range(n8)],
            "b": 0,
        })
    for t in range(32):  # 32 条全稠密耦合行（确定模式，非随机）
        dense_cons.append({
            "coeffs": [1 if ((k * 7 + t * 3) % 5) in (0, 1) else -1
                       for k in range(n8)],
            "b": 10**6 + t,
        })
    dense_audit = {
        "audit_id": "verify-env-dense",
        "variables": [f"I{k}" for k in range(n8)],
        "constraints": dense_cons,
    }
    status, dense_body = http("POST", "/api/audits", dense_audit)
    assert status == 201 and dense_body["result"]["status"] == "feasible"
    dense_env = {
        "envelope_id": "verify-env-toobig",
        "source_audit_id": "verify-env-dense",
        "perturbations": [
            {"u": (i % 3) - 1, "v": ((i // 2) % 3) - 1}
            for i in range(len(dense_cons))
        ],
    }
    status, b = http("POST", "/api/envelopes", dense_env)
    assert status == 422 and b["error"] == "envelope_too_large", (status, b)
    # 拒绝后无残留冻结，原审计仍可读
    assert http("GET", "/api/envelopes/verify-env-toobig")[0] == 404
    assert http("GET", "/api/audits/verify-env-dense")[0] == 200
    print("超受理规模精确拒绝 (422 envelope_too_large) 且无残留")

    # 复用派生编号但改变系数 -> 409，原冻结不变
    changed = json.loads(json.dumps(env_payload))
    changed["perturbations"][0]["u"] = 7
    status, b = http("POST", "/api/envelopes", changed)
    assert status == 409 and b["error"] == "envelope_id_conflict"
    status, frozen = http("GET", "/api/envelopes/verify-env")
    assert status == 200
    fkeys = [(h["alpha"], h["beta"], h["gamma"])
             for h in frozen["projection"]["halfplanes"]]
    assert fkeys == [(-1, 0, 2), (0, -1, 3)]

    # 复用派生编号但改指来源 -> 409
    other_audit = json.loads(json.dumps(audit))
    other_audit["audit_id"] = "verify-env-audit2"
    other_audit["constraints"][0]["b"] = 20
    assert http("POST", "/api/audits", other_audit)[0] == 201
    other_env = dict(env_payload, source_audit_id="verify-env-audit2")
    status, b = http("POST", "/api/envelopes", other_env)
    assert status == 409 and b["error"] == "envelope_id_conflict"

    # 被拒绝的派生编号没有残留记录；原审计指纹不变
    assert http("GET", "/api/envelopes/verify-env-bad")[0] == 404
    assert http("GET", "/api/envelopes/verify-env-inf")[0] == 404
    src_fp = http("GET", "/api/audits/verify-env-audit")[1]["fingerprint"]
    assert src_fp == body2["source_fingerprint"]
    print("来源缺失/不可行/超规、复用编号改来源或系数等拒绝保护核验通过")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"VERIFY FAILED: {exc}", flush=True)
        sys.exit(1)
