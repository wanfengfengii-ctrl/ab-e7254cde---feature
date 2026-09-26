"""最大流 / 最小割引擎、检修审计逻辑与低暴露配流。

规则（对应业务要求）：

* 每条管段被视作一条有向边，录入的最大流量即其容量上限；
* 在**正常网络**与**每一条可检修管段单独移除后的残余网络**上，
  分别独立运行最大流（Dinic），互不复用中间流量结果；
* 事故要求的持续排出流量必须在正常网络和每一个单点失效情景下都可达，
  审计才放行；
* 失效时按管段录入顺序返回第一条不达标管段，并依据最大流 / 最小割定理，
  从残余网络给出可复核的源侧割集、焚烧端侧节点及割集容量。

审计通过后，安全工程师为每条管段录入**非负整数单位暴露代价**，
在正常网络与每个单点失效残余网络中，各分配**恰好等于必须持续排出量**
的流量（被移除管段流量固定为 0），先最小化总暴露代价，再按管段录入
顺序的流量序列字典序稳定决胜（见 :func:`allocate_network`）。

注意：本模块用“流量”而不是“路径条数”下结论——存在多条路径并不保证
总排量达标，共享瓶颈会限制总流量。
"""
from __future__ import annotations

import heapq
import sys
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

# 检修网络节点规模通常不大，放宽递归深度以支持较长的增广链。
sys.setrecursionlimit(100_000)

EPS = 1e-9


class NetworkValidationError(ValueError):
    """网络输入无效（节点引用、容量、方向等业务校验失败）。"""

    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.field = field


@dataclass
class _Edge:
    """Dinic 内部边（带反向残量边索引）。"""

    to: int
    rev: int
    cap: float


class Dinic:
    """容量为非负实数的有向图 Dinic 最大流。"""

    def __init__(self, n: int):
        self.n = n
        self.g: list[list[_Edge]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: float) -> None:
        fwd = _Edge(to=v, rev=len(self.g[v]), cap=float(cap))
        bak = _Edge(to=u, rev=len(self.g[u]), cap=0.0)
        self.g[u].append(fwd)
        self.g[v].append(bak)

    def _bfs(self, s: int, t: int) -> list[int]:
        level = [-1] * self.n
        level[s] = 0
        q = deque([s])
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if e.cap > EPS and level[e.to] < 0:
                    level[e.to] = level[u] + 1
                    q.append(e.to)
        return level

    def _dfs(self, u: int, t: int, pushed: float, level: list[int], it: list[int]) -> float:
        if u == t:
            return pushed
        while it[u] < len(self.g[u]):
            e = self.g[u][it[u]]
            if e.cap > EPS and level[e.to] == level[u] + 1:
                got = self._dfs(e.to, t, min(pushed, e.cap), level, it)
                if got > EPS:
                    e.cap -= got
                    self.g[e.to][e.rev].cap += got
                    return got
            it[u] += 1
        return 0.0

    def max_flow(self, s: int, t: int) -> float:
        flow = 0.0
        inf = float("inf")
        while True:
            level = self._bfs(s, t)
            if level[t] < 0:
                return flow
            it = [0] * self.n
            while True:
                pushed = self._dfs(s, t, inf, level, it)
                if pushed <= EPS:
                    break
                flow += pushed

    def reachable_from_source(self, s: int) -> list[bool]:
        """最大流计算后，沿残余容量 > 0 的边做 BFS，得到源侧节点集合。"""
        seen = [False] * self.n
        seen[s] = True
        q = deque([s])
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if e.cap > EPS and not seen[e.to]:
                    seen[e.to] = True
                    q.append(e.to)
        return seen


def _clean_name(raw, field: str) -> str:
    if not isinstance(raw, str):
        raise NetworkValidationError(f"{field} 必须是字符串", field)
    name = raw.strip()
    if not name:
        raise NetworkValidationError(f"{field} 不能为空", field)
    return name


def _finite_positive_number(raw, field: str) -> float:
    import math

    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise NetworkValidationError(f"{field} 必须是正数", field)
    value = float(raw)
    if not math.isfinite(value):
        raise NetworkValidationError(f"{field} 必须是有限数值", field)
    if value <= 0:
        raise NetworkValidationError(f"{field} 必须大于 0", field)
    return value


def audit_network(
    *,
    source: str,
    sink: str,
    nodes: list[str],
    edges: list[dict],
    required_flow: float,
) -> dict:
    """校验输入并执行正常网络 + 全部单点失效情景的最大流审计。

    ``nodes`` 为汇合节点（及其它中间节点）列表；泄压源与安全焚烧端
    自动并入节点集合。``edges`` 每项形如::

        {"id": "E1" | None, "from": "S", "to": "T",
         "capacity": 100.0, "maintainable": True}

    返回可直接 JSON 序列化的审计结论（见模块 docstring 与 README）。
    """
    import math

    source = _clean_name(source, "泄压源")
    sink = _clean_name(sink, "安全焚烧端")
    if source == sink:
        raise NetworkValidationError("泄压源与安全焚烧端不能是同一节点", "sink")

    if isinstance(required_flow, bool) or not isinstance(required_flow, (int, float)):
        raise NetworkValidationError("事故持续排出流量必须是正数", "required_flow")
    required_flow = float(required_flow)
    if not math.isfinite(required_flow) or required_flow <= 0:
        raise NetworkValidationError("事故持续排出流量必须是大于 0 的有限数值", "required_flow")

    if not isinstance(nodes, list):
        raise NetworkValidationError("汇合节点必须是列表", "nodes")

    node_set: set[str] = {source, sink}
    junction_names: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(nodes):
        field = f"nodes[{i}]"
        name = _clean_name(raw, field)
        if name in seen:
            raise NetworkValidationError(f"汇合节点“{name}”重复", field)
        seen.add(name)
        junction_names.append(name)
        node_set.add(name)

    if not isinstance(edges, list):
        raise NetworkValidationError("管段必须是列表", "edges")

    clean_edges: list[dict] = []
    for i, raw in enumerate(edges):
        if not isinstance(raw, dict):
            raise NetworkValidationError(f"第 {i + 1} 条管段格式无效", f"edges[{i}]")
        label = raw.get("id")
        if label is not None and not (isinstance(label, str) and label.strip()):
            label = None
        elif isinstance(label, str):
            label = label.strip()

        u = _clean_name(raw.get("from"), f"第 {i + 1} 条管段起点")
        v = _clean_name(raw.get("to"), f"第 {i + 1} 条管段终点")
        if u not in node_set:
            raise NetworkValidationError(
                f"第 {i + 1} 条管段起点“{u}”未在节点中定义", f"edges[{i}].from"
            )
        if v not in node_set:
            raise NetworkValidationError(
                f"第 {i + 1} 条管段终点“{v}”未在节点中定义", f"edges[{i}].to"
            )
        if u == v:
            raise NetworkValidationError(
                f"第 {i + 1} 条管段起点和终点不能相同（{u}）", f"edges[{i}].to"
            )
        cap = _finite_positive_number(raw.get("capacity"), f"第 {i + 1} 条管段最大流量")
        maintainable = bool(raw.get("maintainable", False))
        clean_edges.append(
            {
                "index": i,
                "position": i + 1,
                "id": label,
                "from": u,
                "to": v,
                "capacity": cap,
                "maintainable": maintainable,
            }
        )

    all_nodes = sorted(node_set)
    index_of = {name: i for i, name in enumerate(all_nodes)}

    def _solve(removed_index: Optional[int]) -> tuple[float, dict]:
        """在一张**全新**的网络上独立求最大流，并返回流量与最小割证据。"""
        dinic = Dinic(len(all_nodes))
        active = []
        for e in clean_edges:
            if e["index"] == removed_index:
                continue
            dinic.add_edge(index_of[e["from"]], index_of[e["to"]], e["capacity"])
            active.append(e)
        value = dinic.max_flow(index_of[source], index_of[sink])
        side = dinic.reachable_from_source(index_of[source])

        source_side = sorted(all_nodes[k] for k, ok in enumerate(side) if ok)
        sink_side = sorted(all_nodes[k] for k, ok in enumerate(side) if not ok)
        cut_edges = []
        cut_capacity = 0.0
        for e in active:  # 按录入顺序列出，便于复核
            if side[index_of[e["from"]]] and not side[index_of[e["to"]]]:
                cut_edges.append(
                    {
                        "index": e["index"],
                        "position": e["position"],
                        "id": e["id"],
                        "from": e["from"],
                        "to": e["to"],
                        "capacity": _num(e["capacity"]),
                    }
                )
                cut_capacity += e["capacity"]
        return value, {
            "capacity": _num(cut_capacity),
            "source_side_nodes": source_side,
            "sink_side_nodes": sink_side,
            "cut_edges": cut_edges,
        }

    def _meets(value: float) -> bool:
        return value + EPS >= required_flow

    # 1) 正常网络
    normal_value, normal_cut = _solve(None)

    # 2) 每条可检修管段单独临时失效（残余网络独立求解）
    scenarios = []
    failure = None
    for e in clean_edges:
        if not e["maintainable"]:
            continue
        value, cut = _solve(e["index"])
        scenario = {
            "edge_index": e["index"],
            "position": e["position"],
            "edge_id": e["id"],
            "from": e["from"],
            "to": e["to"],
            "capacity": _num(e["capacity"]),
            "max_flow": _num(value),
            "meets": _meets(value),
        }
        scenarios.append(scenario)
        # 按管段录入顺序取首条不达标者
        if failure is None and not _meets(value):
            failure = {
                "stage": "single_failure",
                "edge_index": e["index"],
                "position": e["position"],
                "edge_id": e["id"],
                "from": e["from"],
                "to": e["to"],
                "capacity": _num(e["capacity"]),
                "required_flow": _num(required_flow),
                "max_flow": _num(value),
                "cut": cut,
            }

    # 没有任何可检修管段时，至少正常网络本身必须达标
    if failure is None and not scenarios and not _meets(normal_value):
        failure = {
            "stage": "normal",
            "edge_index": None,
            "position": None,
            "edge_id": None,
            "from": None,
            "to": None,
            "capacity": None,
            "required_flow": _num(required_flow),
            "max_flow": _num(normal_value),
            "cut": normal_cut,
        }

    return {
        "passed": failure is None and _meets(normal_value),
        "required_flow": _num(required_flow),
        "normal": {
            "max_flow": _num(normal_value),
            "meets": _meets(normal_value),
            "cut": normal_cut,
        },
        "scenarios": scenarios,
        "failure": failure,
    }


# ---------------------------------------------------------------------------
# 低暴露配流：最小费用流（整数精确）+ 录入顺序流量序列字典序稳定决胜
# ---------------------------------------------------------------------------


@dataclass
class _CFEdge:
    """连续最短路费用流内部边（带反向边索引）。"""

    to: int
    rev: int
    cap: int
    cost: int


class MinCostFlow:
    """容量、费用、流量均为非负整数的逐次最短路（SSP）最小费用流。

    不假设残量网络无负环：初始网络无反向容量，费用均非负；每轮增广后
    用 Johnson 势函数保证边权（reduced cost）非负，Dijkstra 即可求解。
    取流后通过 ``flow_on`` 读回每条原始管段上的净流量。
    """

    def __init__(self, n: int):
        self.n = n
        self.g: list[list[_CFEdge]] = [[] for _ in range(n)]
        self._origin: list[tuple[int, int]] = []  # (起点, 正向边在 g[u] 中的下标)

    def add_edge(self, u: int, v: int, cap: int, cost: int) -> int:
        idx = len(self._origin)
        fwd = _CFEdge(to=v, rev=len(self.g[v]), cap=cap, cost=cost)
        bak = _CFEdge(to=u, rev=len(self.g[u]), cap=0, cost=-cost)
        self.g[u].append(fwd)
        self.g[v].append(bak)
        self._origin.append((u, len(self.g[u]) - 1))
        return idx

    def min_cost_flow(self, s: int, t: int, required: int) -> Optional[int]:
        """发送恰好 ``required`` 单位流量；不可行返回 None，可行返回总费用。"""
        n = self.n
        potential = [0] * n  # Johnson 势函数
        total_cost = 0
        remaining = required
        INF = None  # 用 None 表示不可达，避免大整数常量
        while remaining > 0:
            dist: list[Optional[int]] = [INF] * n
            prev_v: list[int] = [-1] * n
            prev_e: list[int] = [-1] * n
            dist[s] = 0
            pq: list[tuple[int, int]] = [(0, s)]
            while pq:
                d, u = heapq.heappop(pq)
                if d != dist[u]:
                    continue
                for ei, e in enumerate(self.g[u]):
                    if e.cap <= 0:
                        continue
                    nd = d + e.cost + potential[u] - potential[e.to]
                    if dist[e.to] is None or nd < dist[e.to]:
                        dist[e.to] = nd
                        prev_v[e.to] = u
                        prev_e[e.to] = ei
                        heapq.heappush(pq, (nd, e.to))
            if dist[t] is None:
                return None  # 残余网络已无法继续增广
            for v in range(n):
                if dist[v] is not None:
                    potential[v] += dist[v]
            # 沿最短路尽量增广
            add = remaining
            v = t
            while v != s:
                add = min(add, self.g[prev_v[v]][prev_e[v]].cap)
                v = prev_v[v]
            v = t
            while v != s:
                e = self.g[prev_v[v]][prev_e[v]]
                e.cap -= add
                self.g[v][e.rev].cap += add
                v = prev_v[v]
            total_cost += add * potential[t]  # 增广后的势差即原始最短路费用
            remaining -= add
        return total_cost

    def flow_on(self, edge_index: int) -> int:
        """原始正向管段上的净流量 = 反向残量边上的容量。"""
        u, ei = self._origin[edge_index]
        fwd = self.g[u][ei]
        return self.g[fwd.to][fwd.rev].cap


def _nonneg_int(raw, field: str) -> int:
    """校验“非负整数”：接受 int 与无指数、无尾数的小数字面量（如 3.0）。"""
    if isinstance(raw, bool):
        raise NetworkValidationError(f"{field}必须是非负整数", field)
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not float(raw).is_integer():
            raise NetworkValidationError(f"{field}必须是非负整数", field)
        value = int(raw)
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            value = int(text)
        except ValueError:
            try:
                dec = Decimal(text)
            except Exception:
                raise NetworkValidationError(f"{field}必须是非负整数", field)
            if dec != dec.to_integral_value():
                raise NetworkValidationError(f"{field}必须是非负整数", field)
            value = int(dec)
    else:
        raise NetworkValidationError(f"{field}必须是非负整数", field)
    if value < 0:
        raise NetworkValidationError(f"{field}不能为负数", field)
    return value


def _flow_units(raw, field: str = "事故持续排出流量") -> int:
    """把必须持续排出量换算为整数流量单位。

    容量与要求量沿用审计侧的正数（有限）规则；为保证配流为整数，
    统一按最多 6 位小数放大为整数，超过 6 位小数视为非法（不做静默截断）。
    """
    import math

    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise NetworkValidationError(f"{field}必须是正数", field)
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise NetworkValidationError(f"{field}必须是大于 0 的有限数值", field)
    scaled = Decimal(str(value)).scaleb(6)
    if scaled != scaled.to_integral_value():
        raise NetworkValidationError(f"{field}最多保留 6 位小数", field)
    units = int(scaled)
    if units <= 0:
        raise NetworkValidationError(f"{field}必须大于 0", field)
    return units


def allocate_network(
    *,
    source: str,
    sink: str,
    nodes: list[str],
    edges: list[dict],
    required_flow: float,
) -> dict:
    """审计通过后生成低暴露配流单。

    服务端语义（与业务约定一一对应）：

    1. **先按既有规则重新审计完整草稿**：方向、容量、节点引用等全部
       重新校验，并在正常网络与每个单点失效残余网络上重跑最大流；
    2. 仅当审计放行（``passed``）时才生成配流单，否则返回审计结论，
       ``allocations`` 为 ``None``——调用方继续展示首条失效管段与割集；
    3. 审计通过时，在正常网络与**每条可检修管段单独失效**的残余网络中，
       分别用最小费用流发送**恰好** ``required_flow`` 的流量；被移除管段
       流量固定为 0。每种情形返回各管段流量、总代价与情形编号；
    4. 在所有满足方向、容量与汇合节点守恒的配流中，先取总代价最低，
       再按管段录入顺序的流量序列字典序决胜，得到唯一稳定配流单。

    ``edges`` 每项在审计字段之外还需提供
    ``"exposure_cost": <非负整数>``。
    """
    # 1) 既有规则完整重审（内部含全部业务校验与 400 级异常）
    audit = audit_network(
        source=source,
        sink=sink,
        nodes=nodes,
        edges=edges,
        required_flow=required_flow,
    )

    # 2) 草稿不放行则不得生成配流单：直接带回本次重审结论（首条失效
    #    管段与割集证据），此时不强制要求代价已填写。
    if not audit["passed"]:
        return {
            "passed": False,
            "required_flow": audit["required_flow"],
            "audit": audit,
            "allocations": None,
        }

    required_units = _flow_units(required_flow)

    # 3) 审计放行后重建规范网络，并校验每条管段的非负整数单位暴露代价
    clean_source = str(source).strip()
    clean_sink = str(sink).strip()
    node_set: set[str] = {clean_source, clean_sink}
    for raw in nodes:
        node_set.add(str(raw).strip())
    all_nodes = sorted(node_set)
    index_of = {name: i for i, name in enumerate(all_nodes)}

    clean_edges: list[dict] = []
    for i, raw in enumerate(edges):
        cost = _nonneg_int(
            raw.get("exposure_cost") if isinstance(raw, dict) else None,
            f"第 {i + 1} 条管段单位暴露代价",
        )
        cap_units = _capacity_units(raw["capacity"], f"第 {i + 1} 条管段最大流量")
        clean_edges.append(
            {
                "index": i,
                "position": i + 1,
                "id": (str(raw.get("id")).strip()
                       if isinstance(raw.get("id"), str) and raw.get("id").strip() else None),
                "from": str(raw["from"]).strip(),
                "to": str(raw["to"]).strip(),
                "capacity": cap_units,
                "cost": cost,
                "maintainable": bool(raw.get("maintainable", False)),
            }
        )

    def _solve_case(case_no: int, removed_index: Optional[int]) -> dict:
        """在一张全新网络上求最小费用整数流，并施加录入顺序字典序决胜。"""

        def build(bounds: dict[int, int], probe: Optional[tuple[int, int]] = None):
            """按边界重建费用流网络。

            已决胜管段容量直接收紧为其边界；``probe`` 为 (管段序号, 试探上界)，
            供二分查找该管段在总费用最优前提下可取的最小流量。
            被移除管段不入网（流量固定为 0）。
            """
            mcf = MinCostFlow(len(all_nodes))
            refs: list[Optional[int]] = []
            for g in clean_edges:
                if g["index"] == removed_index:
                    refs.append(None)
                    continue
                cap = g["capacity"]
                if g["index"] in bounds:
                    cap = bounds[g["index"]]
                elif probe is not None and g["index"] == probe[0]:
                    cap = probe[1]
                ref = mcf.add_edge(
                    index_of[g["from"]], index_of[g["to"]], cap, g["cost"]
                )
                refs.append(ref)
            return mcf, refs

        mcf, _ = build({})
        min_total = mcf.min_cost_flow(
            index_of[clean_source], index_of[clean_sink], required_units
        )
        # 审计已保证每种残余网络可达 required_flow，这里仅作防御
        if min_total is None:
            raise NetworkValidationError(
                "配流不可行：当前网络无法送出事故要求流量（请先通过检修审计）",
                None,
            )

        # 字典序决胜：在总费用最优的多解中，按录入顺序依次令每条管段的
        # 流量尽量小（把该管段容量上界收紧后，仍以 min_total 送出要求量）。
        bounds: dict[int, int] = {}
        for e in clean_edges:
            if e["index"] == removed_index:
                continue  # 被移除管段流量固定为 0
            k = e["index"]
            # 已固定前面管段后重解，取该解中 f_k 作为二分上界（它一定是
            # 受限可行集里某个最优解的取值，故不小于真正的最小 f_k）。
            base, base_refs = build(bounds)
            base_total = base.min_cost_flow(
                index_of[clean_source], index_of[clean_sink], required_units
            )
            assert base_total == min_total
            f_k = base.flow_on(base_refs[k])
            lo, hi, best = 0, f_k, f_k
            while lo <= hi:
                mid = (lo + hi) // 2
                trial, _ = build(bounds, (k, mid))
                got = trial.min_cost_flow(
                    index_of[clean_source], index_of[clean_sink], required_units
                )
                if got is not None and got == min_total:
                    best, hi = mid, mid - 1
                else:
                    lo = mid + 1
            bounds[k] = best

        # 用最终全部边界重解一次，得到决胜后的稳定配流
        final, final_refs = build(bounds)
        total = final.min_cost_flow(
            index_of[clean_source], index_of[clean_sink], required_units
        )

        flows: list[dict] = []
        for e in clean_edges:
            ref = final_refs[e["index"]]
            flow_units = 0 if ref is None else final.flow_on(ref)
            flows.append(
                {
                    "index": e["index"],
                    "position": e["position"],
                    "id": e["id"],
                    "from": e["from"],
                    "to": e["to"],
                    "capacity": _num(e["capacity"] / _FLOW_SCALE),
                    "flow": _num(flow_units / _FLOW_SCALE),
                    "exposure_cost": e["cost"],
                    "removed": e["index"] == removed_index,
                }
            )
        return {
            "case_no": case_no,
            "stage": "normal" if removed_index is None else "single_failure",
            "edge_index": None if removed_index is None else removed_index,
            "position": None if removed_index is None else removed_index + 1,
            "edge_id": None if removed_index is None
            else next(e["id"] for e in clean_edges if e["index"] == removed_index),
            "required_flow": audit["required_flow"],
            "total_cost": _num(total / _FLOW_SCALE),
            "flows": flows,
        }

    cases = [_solve_case(1, None)]
    case_no = 2
    for e in clean_edges:
        if e["maintainable"]:
            cases.append(_solve_case(case_no, e["index"]))
            case_no += 1

    return {
        "passed": True,
        "required_flow": audit["required_flow"],
        "audit": audit,
        "allocations": {
            "flow_unit_scale": _FLOW_SCALE,
            "tie_break": "总暴露代价最低；并列时按管段录入顺序的流量序列字典序（前者优先取小）",
            "cases": cases,
        },
    }


_FLOW_SCALE = 10 ** 6


def _capacity_units(raw, field: str) -> int:
    """容量按与要求量相同的 6 位小数刻度换算为整数单位。"""
    import math

    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise NetworkValidationError(f"{field}必须大于 0", field)
    scaled = Decimal(str(value)).scaleb(6)
    if scaled != scaled.to_integral_value():
        raise NetworkValidationError(f"{field}最多保留 6 位小数", field)
    units = int(scaled)
    if units <= 0:
        raise NetworkValidationError(f"{field}必须大于 0", field)
    return units


def _num(x: float) -> float:
    """消除浮点尾差，便于展示与复核（如 0.30000000000000004）。"""
    r = round(float(x), 6)
    return 0.0 if r == 0 else r