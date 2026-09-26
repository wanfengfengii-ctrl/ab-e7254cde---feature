"""最大流 / 最小割引擎与检修审计逻辑。

规则（对应业务要求）：

* 每条管段被视作一条有向边，录入的最大流量即其容量上限；
* 在**正常网络**与**每一条可检修管段单独移除后的残余网络**上，
  分别独立运行最大流（Dinic），互不复用中间流量结果；
* 事故要求的持续排出流量必须在正常网络和每一个单点失效情景下都可达，
  审计才放行；
* 失效时按管段录入顺序返回第一条不达标管段，并依据最大流 / 最小割定理，
  从残余网络给出可复核的源侧割集、焚烧端侧节点及割集容量。

注意：本模块用“流量”而不是“路径条数”下结论——存在多条路径并不保证
总排量达标，共享瓶颈会限制总流量。
"""
from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass
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

    def add_edge(self, u: int, v: int, cap: float) -> int:
        """添加有向边，返回正向边在 ``g[u]`` 中的下标（便于回读实际流量）。"""
        fwd = _Edge(to=v, rev=len(self.g[v]), cap=float(cap))
        bak = _Edge(to=u, rev=len(self.g[u]), cap=0.0)
        self.g[u].append(fwd)
        self.g[v].append(bak)
        return len(self.g[u]) - 1

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

    def max_flow(self, s: int, t: int, limit: float = float("inf")) -> float:
        """求 s→t 最大流；给定 ``limit`` 时流量达到该上限即提前停止。"""
        flow = 0.0
        inf = float("inf")
        while flow < limit - EPS:
            level = self._bfs(s, t)
            if level[t] < 0:
                return flow
            it = [0] * self.n
            while flow < limit - EPS:
                pushed = self._dfs(s, t, min(inf, limit - flow), level, it)
                if pushed <= EPS:
                    break
                flow += pushed
        return flow

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


def _validate_draft(
    *,
    source: str,
    sink: str,
    nodes: list[str],
    edges: list[dict],
    required_flow: float,
) -> dict:
    """按既有规则校验草稿，返回清洗后的节点 / 管段结构。

    检修审计与低暴露配流单共用本校验，保证“提交配流单时先按既有
    规则重新审计完整草稿”。

    ``nodes`` 为汇合节点（及其它中间节点）列表；泄压源与安全焚烧端
    自动并入节点集合。``edges`` 每项形如::

        {"id": "E1" | None, "from": "S", "to": "T",
         "capacity": 100.0, "maintainable": True}
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
    return {
        "source": source,
        "sink": sink,
        "required_flow": required_flow,
        "junction_names": junction_names,
        "edges": clean_edges,
        "all_nodes": all_nodes,
        "index_of": index_of,
    }


def audit_validated_draft(draft: dict) -> dict:
    """在**已校验**的草稿上执行正常网络 + 全部单点失效情景的最大流审计。

    返回可直接 JSON 序列化的审计结论（见模块 docstring 与 README）。
    """
    source = draft["source"]
    sink = draft["sink"]
    required_flow = draft["required_flow"]
    clean_edges = draft["edges"]
    all_nodes = draft["all_nodes"]
    index_of = draft["index_of"]

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
    draft = _validate_draft(
        source=source, sink=sink, nodes=nodes, edges=edges, required_flow=required_flow
    )
    return audit_validated_draft(draft)


def _num(x: float) -> float:
    """消除浮点尾差，便于展示与复核（如 0.30000000000000004）。"""
    r = round(float(x), 6)
    return 0.0 if r == 0 else r
