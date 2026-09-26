# 化工园区事故导排网络 · 检修前审计

安全工程师在检修事故导排总管前，录入：

- **一个泄压源**、**一个安全焚烧端**、若干**汇合节点**；
- 若干带**方向**、**最大流量（容量上限）**、**可检修标记**、
  **非负整数单位暴露代价**的管段；
- 事故时必须持续排出的流量。

系统在**正常网络**与**每一条可检修管段单独临时失效后的残余网络**上，
分别独立运行最大流（Dinic），给出每个情景的**最大可导排量**。
所有情景均不低于事故要求流量才放行；失败时按管段**录入顺序**
返回首条不达标管段，并依据最大流 / 最小割定理给出可复核的
**源侧割集节点、焚烧端侧节点、割集管段与割集容量**。

> 结论以**流量**为准而非路径条数：存在多条路径不代表总排量达标，
> 共享瓶颈会限制总流量（见 `tests/test_flow.py::test_shared_bottleneck_not_path_count`）。

**审计通过后**，系统再按各管段的非负整数单位暴露代价生成事故泄压时的
**低暴露配流单**：在正常网络与每一个单点失效残余网络中分别分配
**恰好等于必须持续排出量**的流量（被移除管段流量固定为 0），在所有
满足方向、容量与汇合节点守恒的配流中先最小化总暴露代价，再按管段
录入顺序的流量序列字典序稳定决胜，避免“只知道可导排，却让更多气体
穿过高风险管段”。提交配流时服务端会**先按既有规则重新审计完整草稿**，
草稿不放行则不生成任何配流单。

## 快速开始（Docker Compose）

```bash
# 构建并启动常驻 Web 服务（默认宿主机端口 8080，可配置）
docker compose up -d --build web
# 浏览器打开 http://localhost:8080

# 自定义宿主机端口
WEB_HOST_PORT=9090 docker compose up -d web
# 或复制 .env.example 为 .env 后修改 WEB_HOST_PORT
```

健康检查：

```bash
curl http://localhost:8080/health
# {"status":"ok","service":"flare-audit", ...}
```

## 一次性交付校验服务 verify

`verify` 是一次性服务：依次运行 **pytest 代码测试 → 构建检查
（字节码编译 + 应用导入）→ 真实拉起 uvicorn 的导排 API 冒烟**，
随后自行退出，**退出码即结论**（0 全部通过，非 0 存在失败项）：

```bash
docker compose build verify
docker compose run --rm verify
echo "exit code = $?"
```

冒烟覆盖：健康检查、达标网络放行、失效网络返回首条失效管段且
割集容量 == 最大流、非法节点引用返回 400，以及低暴露配流 API
（先重审、各情形恰好排出要求量、总代价正确、被移除管段零流、
不放行不出单、非法代价 400）。

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

pytest -q                      # 代码测试
python scripts/verify          # 与容器内一致的一次性校验
uvicorn app.main:app --reload  # 开发服务
```

## 业务 API

### `POST /api/audit`

请求体：

```json
{
  "source": "泄压源V-101",
  "sink": "焚烧炉F-1",
  "required_flow": 95,
  "nodes": ["汇合点A", "汇合点B"],
  "edges": [
    {"id": "E1", "from": "泄压源V-101", "to": "汇合点A", "capacity": 100, "maintainable": true}
  ]
}
```

- `nodes` 仅列汇合节点；泄压源与安全焚烧端自动并入节点集合。
- `capacity` 为管段容量上限，必须为正数；方向为 `from → to`，不可逆向。
- 仅 `maintainable: true` 的管段参与“单管段临时失效”模拟。

响应（失败时节选）：

```json
{
  "passed": false,
  "required_flow": 95,
  "normal": {"max_flow": 100, "meets": true, "cut": { ... }},
  "scenarios": [
    {"position": 1, "edge_id": "E1", "from": "...", "to": "...",
     "capacity": 100, "max_flow": 90, "meets": false}
  ],
  "failure": {
    "stage": "single_failure",
    "position": 1,
    "edge_id": "E1",
    "max_flow": 90,
    "required_flow": 95,
    "cut": {
      "capacity": 90,
      "source_side_nodes": ["泄压源V-101", "汇合点B"],
      "sink_side_nodes": ["汇合点A", "焚烧炉F-1"],
      "cut_edges": [ {"position": 3, "id": "E3", "from": "...", "to": "...", "capacity": 90} ]
    }
  }
}
```

割集可独立复核：把节点按 `source_side_nodes / sink_side_nodes` 两分组，
所有从源侧指向焚烧端侧的管段容量之和应恰为 `capacity`，且依据
最大流 / 最小割定理等于该情景最大可导排量。

输入无效（方向自环、容量非正数、节点引用不存在、源汇相同、必填为空等）
返回 `HTTP 400`：

```json
{"error": "第 1 条管段终点“X”未在节点中定义", "field": "edges[0].to"}
```

### `POST /api/allocate`

审计通过后生成事故泄压低暴露配流单。请求体与 `/api/audit` 相同，
但每条管段必须额外携带 **非负整数** `exposure_cost`（单位暴露代价）：

```json
{
  "source": "泄压源V-101",
  "sink": "焚烧炉F-1",
  "required_flow": 95,
  "nodes": ["汇合点A", "汇合点B"],
  "edges": [
    {"id": "E1", "from": "泄压源V-101", "to": "汇合点A", "capacity": 100,
     "maintainable": true, "exposure_cost": 1}
  ]
}
```

服务端处理顺序：

1. **先按既有规则重新审计完整草稿**（与 `/api/audit` 完全一致），
   草稿非法返回 400；
2. 草稿不放行时 HTTP 200、`passed: false`、`allocations: null`，
   响应中的 `audit` 字段完整带回首条失效管段与最小割证据；
3. 放行后校验每条管段代价为非负整数（否则 400），在**正常网络**
   （情形 1）与**每条可检修管段单独失效的残余网络**（情形 2、3…，
   按录入顺序）中分别用最小费用整数流发送恰好 `required_flow` 的流量，
   被移除管段流量固定为 0。

响应（节选）：

```json
{
  "passed": true,
  "required_flow": 95,
  "audit": { "passed": true, "...": "本次重审的完整审计结论" },
  "allocations": {
    "flow_unit_scale": 1000000,
    "tie_break": "总暴露代价最低；并列时按管段录入顺序的流量序列字典序（前者优先取小）",
    "cases": [
      {
        "case_no": 1,
        "stage": "normal",
        "edge_index": null, "position": null, "edge_id": null,
        "required_flow": 95,
        "total_cost": 190,
        "flows": [
          {"index": 0, "position": 1, "id": "E1", "from": "...", "to": "...",
           "capacity": 100, "flow": 95, "exposure_cost": 1, "removed": false}
        ]
      },
      {
        "case_no": 2,
        "stage": "single_failure",
        "edge_index": 0, "position": 1, "edge_id": "E1",
        "required_flow": 95,
        "total_cost": 950,
        "flows": [ {"...": "被移除管段 removed=true 且 flow=0"} ]
      }
    ]
  }
}
```

- 每种情形的 `total_cost = Σ flow × exposure_cost`；
- 配流满足方向不可逆向、`0 ≤ flow ≤ capacity`、汇合节点流量守恒、
  源点净流出 / 焚烧端净流入恰好等于 `required_flow`；
- 决胜保证：先总代价最低；总代价并列时，按管段录入顺序比较流量序列，
  最靠前出现差异的管段取流量更小者（整数规划的字典序最优解，唯一稳定）；
- 要求量 / 容量允许至多 6 位小数，内部统一放大为整数精确求解，
  不存在浮点尾差；代价为非负整数。

### `GET /health`

容器健康检查端点，返回 `{"status":"ok",...}`。

## 前端交互约定

- 页面分三区：**当前输入（草稿）**、**输入被拒绝（400）**、**审计结论**。
- 管段表含**单位暴露代价**列（非负整数）；另有“生成低暴露配流单”按钮。
- 提交审计后通过真实业务 API 渲染正常网络与逐条失效情形的最大可导排量。
- 通过时显示放行结论；失败时高亮首条失效管段并展示最小割证据。
- 审计通过后可在原审计结论旁直接生成并查看各情形配流（情形 × 管段矩阵，
  含每列总暴露代价；被移除管段以“—”标注）。
- 草稿在上次审计之后被任何修改时，旧结论区顶部出现过期警示，
  旧结论不会被当作新草稿的结果；重新审计后才刷新。
- **草稿或任一代价修改后，旧配流单立即失效**（配流区顶部出现独立过期
  警示）；配流提交始终由服务端先重新审计，草稿不再放行则不生成配流单，
  页面继续展示首条失效管段与割集证据。

## 项目结构

```
app/
  flow.py            # Dinic 最大流 + 残余网络最小割 + 审计编排与业务校验，
                     # 以及最小费用整数流 + 字典序决胜的低暴露配流
  main.py            # FastAPI：/api/audit、/api/allocate、/health、静态页面
  static/            # 原生前端（无构建步骤）
tests/               # pytest：引擎/审计/配流逻辑 + API
scripts/verify       # 一次性校验：测试 + 构建 + API 冒烟（退出码报告）
Dockerfile
docker-compose.yml   # web（常驻，健康检查，端口可配）+ verify（一次性）
```
