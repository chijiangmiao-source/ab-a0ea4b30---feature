# 低温磁阱电流安全审计（coil-audit）

在调整多个线圈电流前，确认安全约束、电源约束与场强线性上界能否**同时**满足。
系统对用户录入的整数不等式 `a·x ≤ b` 给出可复核的精确结论：

* **可行** → 由固定枢轴规则（Bland 规则）得到的一组**有理电流**，以及每条约束的
  **精确余量** `b − a·x`（分数形式，无浮点误差）；
* **无解** → 非负有理 Farkas 乘子，使各约束左侧系数逐项相加为零、右侧和严格为负，
  合并出矛盾式 `0 ≤ -k`，并逐项展示加权合并过程。

核心算法是**任意精度有理数 Phase-I 单纯形**（Python `fractions.Fraction`），
不使用浮点、采样、外部线性规划服务，也不会在无解时只报告“无解”而不给证据。

在可行审计之上还可以建立**运行包线**：为每条原约束填两个整数扰动系数
`(u_i, v_i)`，约束随功率漂移 `p` 与场强偏置 `q` 变为 `a_i·x ≤ b_i + u_i·p + v_i·q`。
系统用**任意精度有理数 Fourier–Motzkin 消元**逐一代数消去全部电流自由变量，
一次性把**全部可行扰动对**精确投影成规范整数半平面集合 `α·p + β·q ≤ γ`，
每条边界附带对原约束的非负有理乘子（来源合并式）。包线同样冻结、可重放、
可按编号取回，并支持包线内/外有理坐标的精确查询。

## 运行

```bash
# 宿主机端口可配置（默认 8080）
HOST_PORT=9090 docker compose up --build
# 浏览器打开 http://localhost:9090
```

健康状态：`GET /healthz` → `{"status":"ok",...}`（Compose 以该端点作为
服务健康条件）。冻结记录持久化在命名卷 `audit-data` 中。

### verify 单次容器

```bash
make verify          # = docker compose build
                     #   && docker compose up --abort-on-container-exit \
                     #                      --exit-code-from verify
echo $?              # 0 表示全部通过
```

`verify` 容器（`restart: "no"`）在 web 健康后运行一次并退出：

1. 容器内执行全部代码测试（pytest）；
2. HTTP 冒烟：健康检查、首页、未知编号返回 404；
3. 用两组“相加得 `0 ≤ -1`”的约束（`0·x ≤ -1`；`x ≤ 0` 与 `-x ≤ -1`）
   经真实 HTTP 提交，独立重算并核对 `μ ≥ 0`、`Σμ·a = 0`、`Σμ·b = -1`，
   同时核对幂等重放（200，同一记录）与改动载荷冲突（409，原证据不变）；
4. 再提交一个可行系统核对有理解与余量；
5. **运行包线**：经真实 HTTP 创建冻结包线，独立复算每条规范半平面的来源
   合并式（`λ ≥ 0`、`Σλ·a = 0`、与规范行严格正比例）；对包线内有理坐标
   回算电流与余量、对包线外坐标核对首条违约边界/严格违反值/代入结果（并与
   verify 内独立的 FM 判定交叉一致）；核对来源缺失(404)、来源不可行(422)、
   超受理规模(422 `envelope_too_large`，无残留)、复用编号改系数或改来源
   (409，原冻结不变)、非法载荷(400) 等全部拒绝保护。

任一步失败即以非零码退出；全部通过输出 `VERIFY OK` 并以 **0** 退出。
镜像构建本身由 `make verify` 的 `docker compose build` 完成，失败同样中止。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/audits` | 提交审计（JSON 见下） |
| `GET`  | `/api/audits/{audit_id}` | 按编号重新读取冻结结果 |
| `POST` | `/api/envelopes` | 基于可行审计创建/重放运行包线 |
| `GET`  | `/api/envelopes/{envelope_id}` | 按编号读取冻结包线 |
| `POST` | `/api/envelopes/{envelope_id}/query` | 包线内有理坐标查询 |
| `GET`  | `/healthz` | 健康检查 |

提交载荷（1–8 个变量，1–48 条约束；系数与右端必须是 **JSON 整数**，
`1.0`、`true`、`"3"` 一律拒绝）：

```json
{
  "audit_id": "trap-run-2026-09-28-A",
  "variables": ["I1", "I2"],
  "constraints": [
    {"coeffs": [1, 1], "b": 40, "label": "电源总电流上限", "stable": true},
    {"coeffs": [2, -1], "b": 10, "label": "场强线性上界", "stable": false}
  ]
}
```

* 首次提交 → `201`，结论与载荷指纹一起冻结落盘；
* 相同编号 + **完全相同载荷**重传 → `200`，返回同一条冻结记录（含相同
  `created_at` 与 `fingerprint`）；
* 相同编号 + **改动过的载荷**（任何系数/右端/标签/稳定标识/变量名变化）
  → `409 Conflict`，响应给出新旧指纹，**原证据不变**；
* 非法载荷 → `400`，不求解、不写入，任何旧结论都不会残留或被覆盖。

无解响应示例（节选）：

```json
{
  "result": {
    "status": "infeasible",
    "multipliers": [{"index": 0, "value": "1"}],
    "terms": [{
      "index": 0, "b": "-1", "multiplier": "1",
      "weighted_coeffs": ["0"], "weighted_rhs": "-1"
    }],
    "combined_lhs": ["0"],
    "combined_rhs": "-1",
    "combined_relation": "0 <= -1"
  }
}
```

### 运行包线

创建载荷（`perturbations` 必须**恰好每条原约束一对**整数 `(u, v)`，顺序与
来源审计一致；数量不等、浮点/布尔/字符串数字一律 `400`）：

```json
{
  "envelope_id": "trap-run-A-envelope",
  "source_audit_id": "trap-run-2026-09-28-A",
  "perturbations": [
    {"u": 1, "v": 0},
    {"u": 0, "v": 1}
  ]
}
```

* 来源审计不存在 → `404 source_not_found`；来源审计不可行 →
  `422 source_infeasible`；投影中间规模超出受理能力 →
  `422 envelope_too_large`（**精确拒绝，不抽样、不近似、不写入**）；
* 首次成功 → `201`；同编号 + 同来源指纹 + 同系数重传 → `200` 同一冻结记录；
* **复用派生编号但改变来源或任一对扰动系数** → `409 envelope_id_conflict`，
  响应给出新旧指纹，**原冻结包线不变**；包线的规格指纹把
  `source_fingerprint` 一并纳入，因此换绑来源必然冲突；
* 创建包线只**读**审计存储，原审计记录的读取、精确余量、无解证书完全不受影响。

冻结结果含规范整数半平面（无任何半平面时 `whole_plane: true`，表示任意
`(p, q)` 都可行），每条边界给出系数、可读 `line`、对全部原约束的非负有理
`multipliers`，以及逐项展开的 `derivation` 来源合并式（含消净后全 0 的
电流系数与合并出的 `p/q/rhs`）。例：

```json
{
  "projection": {
    "method": "exact-rational/fourier-motzkin",
    "whole_plane": false,
    "halfplanes": [
      {"index": 0, "alpha": -1, "beta": 0, "gamma": 2, "line": "-p <= 2",
       "multipliers": ["1", "0", "1", "0"],
       "derivation": {"terms": [ ... ], "combined_p": "-1",
                      "combined_q": "0", "combined_rhs": "2"}}
    ],
    "boundary_count": 2,
    "combinations_per_stage": [2, 1],
    "elimination_order": [0, 1]
  }
}
```

坐标查询 `POST /api/envelopes/{id}/query`：`p`、`q` 接受 JSON 整数或**精确
有理文本**（`"3"`、`"-1/2"`、`"1.25"`，按文本原样解析，拒绝 JSON 浮点
`0.5`、`"1/0"`、指数记法等）。等号落在边界上视为可行。

* 包线内 → `"feasible": true`，给出一组由精确 Phase-I 单纯形算出的
  **可复算有理电流**，以及每条原约束在该 `(p, q)` 下的扰动后右端与精确余量；
* 包线外 → `"feasible": false`，按冻结规范顺序稳定给出**首条违约边界**
  `index`、`line`、代入结果 `substitution = α·p + β·q`、严格大于 `γ` 的
  **严格违反值** `strict_excess`（>0），并附该边界的来源合并式证据。

## 实现说明

* `app/simplex.py` — 有理数 Phase-I 单纯形：
  自由变量统一拆成 `u − v`；右端为负的行先整体乘 `-1`（记 `σ = sign(b)`，
  对应松弛/剩余变量系数同为 `σ`），加入人工变量得到对任意整数右端都合法的初始基；
  最小化人工变量之和，按 **Bland 固定枢轴规则**（编号最小的负约化成本列入基，
  最小比值、并列时基变量编号最小者出基；人工列永不重新入基）防止退化循环。
  终表目标行松弛/剩余列的约化成本即 Farkas 乘子，代码对 `μ ≥ 0`、
  `μᵀA = 0`、`μᵀb = −w* < 0` 做运行时断言。
* `app/models.py` — 整数级输入校验（拒绝浮点/布尔/字符串数字、超大整数）。
* `app/storage.py` — 冻结存储：`AuditStore`（审计）与 `EnvelopeStore`
  （包线，独立 `envelopes/` 子目录）。每编号一个 JSON，临时文件 + fsync +
  原子 rename，进程内锁串行化“检查-写入”，指纹为规范 JSON 的 SHA-256。
  包线规格指纹把**来源指纹**一并纳入，复用编号改来源/改系数必然 409。
* `app/envelope.py` — 精确有理数 **Fourier–Motzkin 投影**：把
  `a·x − u·p − v·q ≤ b` 的电流自由变量逐代数消去，得到规范整数半平面。
  每阶段做公分母去分母 + 整体 gcd 的**只正不归一**（绝不乘 −1，否则会把
  非负来源乘子翻负并错误合并相反半平面）；比例重复行按整数元组去重，只保留
  唯一代表与合法来源；零系数行直接传递，常量 `0≤非负` 丢弃，`0≤负数` 即
  来源不可行的 Farkas 证书（与“来源可行”矛盾，按内部错误处理）。
  消元顺序用**无损贪心**（每步选“上界数×下界数”最小、并列取编号最小的
  变量）抑制中间膨胀——FM 投影与顺序无关，故只影响性能不改变结果；
  任一阶段不同行数、候选合并数或系数位宽超过受理上限即抛
  `EnvelopeScaleError` 拒绝出证，不抽样、不浮点、不近似。无界方向只是
  “没有生成边界”，不会被人为封闭。每条半平面保留非负有理乘子，并在产出时
  用原始约束独立重算 `Σλ·a = 0`、合并式与规范行严格正比例。
* `app/envelope_models.py` — 包线载荷的整数级校验与坐标的精确有理文本解析
  （`Fraction`，拒绝浮点/零分母/指数记法，限位宽）。
* `app/envelope_service.py` — 包线编排：只读取来源审计 → 校验 → 精确投影 →
  冻结/重放；查询时从冻结记录重建投影（不重新消元），包线内调用精确
  Phase-I 单纯形求可复算电流并回代核验，包线外给首条违约边界与严格违反值。
* `app/web.py` — 仅依赖 Python 标准库的 HTTP 服务；`app/static/index.html`
  为浏览器页面（整数以文本正则校验后直接拼装 JSON，避免 JS `number` 丢精度；
  坐标查询同样以文本原样提交，不经 `Number()`）。
* `tests/` — 226 个测试；其中 `tests/fourier_motzkin.py` 用与单纯形完全不同的
  Fourier–Motzkin 消元对大量随机/枚举小系统做交叉判定，并独立核验证书；
  包线引擎测试进一步把投影成员判定与“具体化 (p,q) 后独立 FM 判定”在大量
  有理点上逐一对照，并独立复算每条半平面的来源合并式。

## 本地（无 Docker）

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest -q
AUDIT_PORT=8091 AUDIT_DATA_DIR=./data .venv/bin/python -m app.web
WEB_URL=http://127.0.0.1:8091 VERIFY_SRC_DIR="$PWD" \
  .venv/bin/python scripts/verify.py
```
