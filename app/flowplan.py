"""低暴露配流单：事故泄压时的最小代价配流。

检修前审计放行之后，安全工程师为当前草稿的每条管段填写**非负整数
单位暴露代价**，本模块据此生成低暴露配流单，避免“仅知道可导排，
却让更多气体穿过高风险管段”。

规则（对应业务要求）：

* 提交时先按既有规则重新审计完整草稿；草稿不放行则不生成配流单；
* 在**正常网络**与**每一条可检修管段单独失效后的残余网络**上，
  分别分配**恰好等于**事故必须持续排出量的流量，被移除管段流量
  固定为零；
* 在所有满足方向、容量上限与汇合节点流量守恒的配流中，先取
  **总代价最低**（Σ 流量 × 单位暴露代价）者；
* 总代价并列时，按**管段录入顺序**比较流量序列，取字典序最小者
  （稳定决胜：排在前面的管段尽量少承担流量；结果只取决于录入
  顺序，与求解器内部细节无关，可复现）。

实现要点：

* 最小代价用 SSP（连续最短路 + 势量）求解。单位代价是非负整数，
  最短路距离与势量始终保持整数，浮点可精确表示，不引入误差；
* 字典序决胜在“零费用残余环”上完成：最小代价流的最优性保证残余
  图中不存在负费用环，因此沿费用为 0 的残余环改推流量不会改变
  总代价；而任意两个最优流之差都可分解为零费用残余环。按录入
  顺序依次用零费用环把每条管段的流量压到最小并锁定，即得真正
  字典序最小的最优配流；
* 每个情景都在**全新**的网络上独立求解，互不复用中间流量结果。
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from .flow import (
    EPS,
    Dinic,
    NetworkValidationError,
    _num,
    _validate_draft,
    audit_validated_draft,
)


@dataclass
class _MCFEdge:
    """最小费用流内部边（带反向残量边索引与整数单位代价）。"""

    to: int
    rev: int
    cap: float
    cost: int


class MinCostFlow:
    """单位代价为非负整数、容量为非负实数的最小费用流（SSP）。

    代价恒为整数：最短距离、势量、路径费用都是整数的浮点表示，
    比较与累加精确无误差。
    """

    def __init__(self, n: int):
        self.n = n
        self.g: list[list[_MCFEdge]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: float, cost: int) -> int:
        """添加有向边，返回正向边在 ``g[u]`` 中的下标（便于回读流量）。"""
        fwd = _MCFEdge(to=v, rev=len(self.g[v]), cap=float(cap), cost=int(cost))
        bak = _MCFEdge(to=u, rev=len(self.g[u]), cap=0.0, cost=-int(cost))
        self.g[u].append(fwd)
        self.g[v].append(bak)
        return len(self.g[u]) - 1

    def min_cost_flow(self, s: int, t: int, amount: float):
        """从 s 向 t 输送恰好 ``amount`` 的流量并最小化总代价。

        返回 ``(实际流量, 总代价, 势量)``。若网络最大可输送量不足
        ``amount``，则返回实际可达流量（调用方应先以最大流审计保证
        可行）。返回的势量满足：所有残余边的约化费用
        ``cost + π[u] − π[v]`` 均 ≥ 0（最小费用流最优性条件）。
        """
        n = self.n
        potential = [0] * n
        flow = 0.0
        total_cost = 0.0
        inf = float("inf")
        while flow < amount - EPS:
            # Dijkstra（约化费用非负，由势量保证）
            dist = [inf] * n
            dist[s] = 0
            prev_node = [-1] * n
            prev_edge = [-1] * n
            pq: list[tuple[float, int]] = [(0, s)]
            while pq:
                d, u = heapq.heappop(pq)
                if d > dist[u]:
                    continue
                for ei, e in enumerate(self.g[u]):
                    if e.cap <= EPS:
                        continue
                    nd = d + e.cost + potential[u] - potential[e.to]
                    if nd < dist[e.to]:
                        dist[e.to] = nd
                        prev_node[e.to] = u
                        prev_edge[e.to] = ei
                        heapq.heappush(pq, (nd, e.to))
            if dist[t] == inf:
                break
            for v in range(n):
                if dist[v] < inf:
                    potential[v] += dist[v]
            # 沿最短路推送尽可能多（但不超过本次缺口）的流量
            push = amount - flow
            v = t
            while v != s:
                e = self.g[prev_node[v]][prev_edge[v]]
                push = min(push, e.cap)
                v = prev_node[v]
            path_cost = 0
            v = t
            while v != s:
                e = self.g[prev_node[v]][prev_edge[v]]
                e.cap -= push
                self.g[v][e.rev].cap += push
                path_cost += e.cost
                v = prev_node[v]
            flow += push
            total_cost += push * path_cost
        return flow, total_cost, potential


@dataclass
class _PlanEdge:
    """配流计算中一条管段的内部引用。"""

    u: int
    v: int
    cap: float
    cost: int
    fwd_idx: int  # 正向残余边在 MinCostFlow.g[u] 中的下标


def _flow_of(mcf: MinCostFlow, pe: _PlanEdge) -> float:
    return pe.cap - mcf.g[pe.u][pe.fwd_idx].cap


def _lexicographic_minimize(
    mcf: MinCostFlow, plan_edges: list[_PlanEdge], potential: list[int]
) -> None:
    """在保持总代价不变的前提下，按列表顺序逐条把流量压到字典序最小。

    最优性保证残余图无负费用环，故费用为 0 的残余环保持总代价不变；
    对每条管段 i，在“零约化费用残余图（不含已锁定管段与本管段）”中
    求 起点→终点 的最大可改流量 δ，把 δ 沿“路径 + 本管段反向边”构成
    的零费用环推回，即把本管段流量降到最小，随后锁定本管段。
    """
    locked: set[int] = set()
    for i, pe in enumerate(plan_edges):
        current = _flow_of(mcf, pe)
        if current <= EPS:
            locked.add(i)
            continue
        fwd_i = mcf.g[pe.u][pe.fwd_idx]
        rc_fwd = fwd_i.cost + potential[pe.u] - potential[pe.v]
        # 只有本管段前向弧约化费用恰为 0 时，沿零约化费用路径改推
        # 才构成零费用环（总代价不变）。饱和且更便宜的紧瓶颈边
        # rc_fwd < 0：任何替代路径都更贵，不能压流，直接锁定。
        if rc_fwd != 0:
            locked.add(i)
            continue
        # 零约化费用残余图（排除已锁定管段与本管段）
        dinic = Dinic(mcf.n)
        arcs: list[tuple[int, bool, int, int, float]] = []
        for j, qe in enumerate(plan_edges):
            if j == i or j in locked:
                continue
            fwd = mcf.g[qe.u][qe.fwd_idx]
            bak = mcf.g[qe.v][fwd.rev]
            # 正向残余弧（该管段流量可增）
            if fwd.cap > EPS and fwd.cost + potential[qe.u] - potential[qe.v] == 0:
                idx = dinic.add_edge(qe.u, qe.v, fwd.cap)
                arcs.append((j, True, qe.u, idx, fwd.cap))
            # 反向残余弧（该管段流量可减）
            if bak.cap > EPS and bak.cost + potential[qe.v] - potential[qe.u] == 0:
                idx = dinic.add_edge(qe.v, qe.u, bak.cap)
                arcs.append((j, False, qe.v, idx, bak.cap))
        # 最多把本管段当前流量全部改推出去
        moved = dinic.max_flow(pe.u, pe.v, limit=current)
        if moved > EPS:
            # 把 Dinic 实际推送的流量折算回真实残余网络
            for j, is_fwd, au, aidx, acap in arcs:
                used = acap - dinic.g[au][aidx].cap
                if used <= EPS:
                    continue
                qe = plan_edges[j]
                fwd = mcf.g[qe.u][qe.fwd_idx]
                bak = mcf.g[qe.v][fwd.rev]
                if is_fwd:
                    fwd.cap -= used
                    bak.cap += used
                else:
                    bak.cap -= used
                    fwd.cap += used
            # 本管段流量减少 moved（正向残余增大、反向残余减小）
            fwd_i = mcf.g[pe.u][pe.fwd_idx]
            bak_i = mcf.g[pe.v][fwd_i.rev]
            fwd_i.cap += moved
            bak_i.cap -= moved
        locked.add(i)


def _validate_costs(raw_edges: list) -> list[int]:
    """逐条校验单位暴露代价：必须填写且为非负整数。

    JSON 的 ``5`` 与 ``5.0`` 都视为整数；布尔、字符串、小数、负数、
    缺失一律拒绝。与管段录入顺序对齐返回整数代价列表。
    """
    costs: list[int] = []
    for i, raw in enumerate(raw_edges):
        field = f"edges[{i}].cost"
        label = f"第 {i + 1} 条管段单位暴露代价"
        value = raw.get("cost") if isinstance(raw, dict) else None
        if value is None:
            raise NetworkValidationError(f"{label}必须填写（非负整数）", field)
        if isinstance(value, bool):
            raise NetworkValidationError(f"{label}必须是非负整数", field)
        if isinstance(value, int):
            cost = value
        elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
            cost = int(value)
        else:
            raise NetworkValidationError(f"{label}必须是非负整数", field)
        if cost < 0:
            raise NetworkValidationError(f"{label}不能为负数", field)
        costs.append(cost)
    return costs


def _solve_plan(draft: dict, removed_index: int | None) -> dict:
    """在一张**全新**的网络上独立求解一个情景的低暴露配流。"""
    all_nodes = draft["all_nodes"]
    index_of = draft["index_of"]
    source = draft["source"]
    sink = draft["sink"]
    required = draft["required_flow"]

    mcf = MinCostFlow(len(all_nodes))
    plan_edges: list[tuple[int, _PlanEdge]] = []  # (管段录入下标, 内部引用)
    for e in draft["edges"]:
        if e["index"] == removed_index:
            continue  # 被移除管段不进入残余网络，流量固定为零
        u, v = index_of[e["from"]], index_of[e["to"]]
        fwd_idx = mcf.add_edge(u, v, e["capacity"], e["cost"])
        plan_edges.append((e["index"], _PlanEdge(u, v, e["capacity"], e["cost"], fwd_idx)))

    flow, _, potential = mcf.min_cost_flow(index_of[source], index_of[sink], required)
    if flow + 1e-6 < required:  # 审计已放行时不可达（最大流 ≥ 要求）
        raise RuntimeError("残余网络无法输送事故必须持续排出量，与审计结论不一致")

    # 总代价并列时按录入顺序取流量序列字典序最小者
    _lexicographic_minimize(mcf, [pe for _, pe in plan_edges], potential)

    flow_of: dict[int, float] = {
        idx: _flow_of(mcf, pe) for idx, pe in plan_edges
    }
    flows = []
    total_cost = 0.0
    for e in draft["edges"]:
        removed = e["index"] == removed_index
        f = 0.0 if removed else flow_of[e["index"]]
        if abs(f) < EPS:
            f = 0.0
        total_cost += f * e["cost"]
        flows.append(
            {
                "position": e["position"],
                "edge_id": e["id"],
                "from": e["from"],
                "to": e["to"],
                "capacity": _num(e["capacity"]),
                "cost": e["cost"],
                "flow": _num(f),
                "exposure": _num(f * e["cost"]),
                "removed": removed,
            }
        )
    # 以泄压源净流出量为准回报实际配流量（应恰为事故要求流量）
    flow_value = sum(
        (f["flow"] if f["from"] == source else 0.0) - (f["flow"] if f["to"] == source else 0.0)
        for f in flows
    )
    return {
        "flow_value": _num(flow_value),
        "total_cost": _num(total_cost),
        "flows": flows,
    }


def plan_low_exposure(
    *,
    source: str,
    sink: str,
    nodes: list[str],
    edges: list[dict],
    required_flow: float,
) -> dict:
    """先按既有规则重新审计完整草稿，再生成低暴露配流单。

    除既有草稿字段外，每条管段必须携带 ``cost``（非负整数单位暴露
    代价）。返回结构::

        {
          "passed": bool,
          "required_flow": float,
          "audit": {...},        # 既有审计结论（含失败时的割集证据）
          "plans": [             # 仅在审计放行时生成，否则为 None
            {"scenario": 0, "stage": "normal", "removed": None,
             "flow_value": 95, "total_cost": 190, "flows": [...]},
            {"scenario": 1, "stage": "single_failure",
             "removed": {"position": 1, ...}, ...},
          ] | None,
        }

    情形编号：0 为正常网络，1..k 为可检修管段按录入顺序逐一失效。
    """
    # 1) 先按既有规则重新校验并审计完整草稿（草稿问题优先于代价问题）
    draft = _validate_draft(
        source=source, sink=sink, nodes=nodes, edges=edges, required_flow=required_flow
    )
    # 2) 单位暴露代价校验（非负整数，逐条必填）
    for e, cost in zip(draft["edges"], _validate_costs(edges)):
        e["cost"] = cost
    # 3) 既有规则重新审计；不放行则不得生成配流单
    audit = audit_validated_draft(draft)
    result = {
        "passed": audit["passed"],
        "required_flow": audit["required_flow"],
        "audit": audit,
        "plans": None,
    }
    if not audit["passed"]:
        return result

    # 4) 正常网络（情形 0）+ 每条可检修管段单独失效（情形 1..k）
    plans = []
    normal = _solve_plan(draft, None)
    normal.update({"scenario": 0, "stage": "normal", "removed": None})
    plans.append(normal)
    for s in audit["scenarios"]:
        plan = _solve_plan(draft, s["edge_index"])
        plan.update(
            {
                "scenario": len(plans),
                "stage": "single_failure",
                "removed": {
                    "position": s["position"],
                    "edge_id": s["edge_id"],
                    "from": s["from"],
                    "to": s["to"],
                },
            }
        )
        plans.append(plan)
    result["plans"] = plans
    return result
