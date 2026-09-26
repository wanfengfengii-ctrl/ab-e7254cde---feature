"""最大流引擎与审计逻辑测试。

重点验证业务约束：
* 管段容量是上限，方向不可逆向；
* 用流量而非路径条数下结论（共享瓶颈场景）；
* 每个单点失效情景在独立残余网络上求解；
* 失败时按录入顺序返回首条失效管段，且割集容量 == 该情景最大流；
* 审计通过后的低暴露配流：恰好排出要求量、总代价最低、
  并列时按录入顺序流量序列字典序稳定决胜，移除管段流量固定为 0。
"""
import pytest

from app.flow import NetworkValidationError, allocate_network, audit_network


def _base_edges():
    """两条 100 干线并联：S→A→T 与 S→B→T。"""
    return [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
        {"id": "E3", "from": "S", "to": "B", "capacity": 100, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 100, "maintainable": True},
    ]


def test_normal_and_each_single_failure_independent():
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=_base_edges(), required_flow=95)
    assert r["passed"] is True
    assert r["normal"]["max_flow"] == 200
    assert len(r["scenarios"]) == 4
    # 任一干线管段失效后仍剩 100（独立求解，不串流量）
    assert all(s["max_flow"] == 100 and s["meets"] for s in r["scenarios"])
    assert r["failure"] is None


def test_shared_bottleneck_not_path_count():
    """两条路径共享 50 瓶颈：路径数=2 但最大流只有 50。"""
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": False},
        {"id": "E2", "from": "S", "to": "B", "capacity": 100, "maintainable": False},
        {"id": "E3", "from": "A", "to": "C", "capacity": 100, "maintainable": False},
        {"id": "E4", "from": "B", "to": "C", "capacity": 100, "maintainable": False},
        {"id": "E5", "from": "C", "to": "T", "capacity": 50, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=["A", "B", "C"],
                      edges=edges, required_flow=60)
    assert r["normal"]["max_flow"] == 50
    assert r["passed"] is False
    # 无可检修管段时，正常网络不达标也要报失败
    assert r["failure"]["stage"] == "normal"
    assert r["failure"]["cut"]["capacity"] == 50


def test_first_failing_edge_by_input_order_and_cut():
    """首条失效管段按录入顺序；割集容量等于该情景最大流。"""
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
        {"id": "E3", "from": "S", "to": "B", "capacity": 90, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 90, "maintainable": True},
    ]
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=edges, required_flow=95)
    assert r["passed"] is False
    f = r["failure"]
    assert f["stage"] == "single_failure"
    assert f["position"] == 1 and f["edge_id"] == "E1"
    assert f["max_flow"] == 90
    cut = f["cut"]
    assert cut["capacity"] == 90 == f["max_flow"]          # 最大流 = 最小割
    assert "S" in cut["source_side_nodes"]
    assert "T" in cut["sink_side_nodes"]
    assert len(cut["cut_edges"]) >= 1
    # 割边方向必须源侧 → 焚烧端侧
    for e in cut["cut_edges"]:
        assert e["from"] in cut["source_side_nodes"]
        assert e["to"] in cut["sink_side_nodes"]
    # 情景表仍按录入顺序完整列出
    assert [s["position"] for s in r["scenarios"]] == [1, 2, 3, 4]
    assert r["scenarios"][0]["meets"] is False
    assert r["scenarios"][1]["meets"] is False
    assert r["scenarios"][2]["meets"] is True


def test_direction_is_enforced():
    """方向不可逆向：T→S 的边不能用来从 S 导流到 T。"""
    edges = [
        {"id": "E1", "from": "T", "to": "S", "capacity": 100, "maintainable": True},
    ]
    r = audit_network(source="S", sink="T", nodes=[], edges=edges, required_flow=1)
    assert r["normal"]["max_flow"] == 0
    assert r["passed"] is False


def test_capacity_is_upper_bound_parallel_edges_sum():
    edges = [
        {"id": "E1", "from": "S", "to": "T", "capacity": 30, "maintainable": True},
        {"id": "E2", "from": "S", "to": "T", "capacity": 70, "maintainable": True},
    ]
    # 正常网络：并联容量相加 = 100
    r = audit_network(source="S", sink="T", nodes=[], edges=edges, required_flow=100)
    assert r["normal"]["max_flow"] == 100
    assert r["normal"]["meets"] is True
    # 但任一可检修管段失效后只剩另一条，整体审计不得通过
    assert r["passed"] is False
    # 要求 71：移除第 1 条(30) 后只剩 70，按录入顺序首条失效即 #1
    r2 = audit_network(source="S", sink="T", nodes=[], edges=edges, required_flow=71)
    assert r2["passed"] is False
    assert r2["failure"]["position"] == 1
    assert r2["failure"]["max_flow"] == 70


def test_non_maintainable_edges_not_simulated():
    edges = _base_edges() + [
        {"id": "E5", "from": "A", "to": "B", "capacity": 10, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=edges, required_flow=95)
    assert len(r["scenarios"]) == 4  # E5 不参与失效模拟
    assert all(s["edge_id"] != "E5" for s in r["scenarios"])


def test_junction_nodes_and_source_sink_auto_registered():
    """泄压源/焚烧端不必出现在 nodes 列表中。"""
    r = audit_network(source="S", sink="T", nodes=[],
                      edges=[{"from": "S", "to": "T", "capacity": 5, "maintainable": False}],
                      required_flow=5)
    assert r["passed"] is True
    assert r["normal"]["max_flow"] == 5


@pytest.mark.parametrize(
    "kwargs, needle",
    [
        (dict(source="S", sink="S", nodes=[], edges=[], required_flow=1), "不能是同一节点"),
        (dict(source="S", sink="T", nodes=["A", "A"], edges=[], required_flow=1), "重复"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "X", "capacity": 1}], required_flow=1), "未在节点中定义"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "X", "to": "T", "capacity": 1}], required_flow=1), "未在节点中定义"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "S", "capacity": 1}], required_flow=1), "起点和终点不能相同"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "T", "capacity": 0}], required_flow=1), "大于 0"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "T", "capacity": -3}], required_flow=1), "大于 0"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "T", "capacity": "大"}], required_flow=1), "正数"),
        (dict(source="S", sink="T", nodes=[], edges=[], required_flow=0), "大于 0"),
        (dict(source="S", sink="T", nodes=[], edges=[], required_flow=-1), "大于 0"),
        (dict(source="", sink="T", nodes=[], edges=[], required_flow=1), "不能为空"),
    ],
)
def test_invalid_inputs_rejected(kwargs, needle):
    with pytest.raises(NetworkValidationError) as exc:
        audit_network(**kwargs)
    assert needle in str(exc.value)


def test_cut_capacity_equals_max_flow_normal():
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 40, "maintainable": True},
        {"id": "E2", "from": "S", "to": "B", "capacity": 60, "maintainable": True},
        {"id": "E3", "from": "A", "to": "T", "capacity": 50, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 50, "maintainable": True},
    ]
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=edges, required_flow=1)
    # S→A 限 40，B→T 限 50，合计 90
    assert r["normal"]["max_flow"] == 90
    assert r["normal"]["cut"]["capacity"] == 90


# ---------------------------------------------------------------------------
# 低暴露配流
# ---------------------------------------------------------------------------


def _allocation_edges():
    """两条 100 干线：经 A 代价低(1)，经 B 代价高(5)。"""
    return [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True, "exposure_cost": 1},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True, "exposure_cost": 1},
        {"id": "E3", "from": "S", "to": "B", "capacity": 100, "maintainable": True, "exposure_cost": 5},
        {"id": "E4", "from": "B", "to": "T", "capacity": 100, "maintainable": True, "exposure_cost": 5},
    ]


def _check_case(case, edges, required, removed_index=None):
    """校验单个情形：方向、容量、守恒、恰好排出、总代价、移除管段零流。"""
    flows = {f["index"]: f for f in case["flows"]}
    balance = {}
    for i, e in enumerate(edges):
        f = flows[i]
        assert f["id"] == e["id"]
        assert 0 <= f["flow"] <= e["capacity"] + 1e-9
        if i == removed_index:
            assert f["removed"] is True and f["flow"] == 0
        else:
            assert f["removed"] is False
        balance[e["from"]] = balance.get(e["from"], 0.0) - f["flow"]
        balance[e["to"]] = balance.get(e["to"], 0.0) + f["flow"]
    assert abs(balance["S"] + required) < 1e-6
    assert abs(balance["T"] - required) < 1e-6
    for node, b in balance.items():
        if node not in ("S", "T"):
            assert abs(b) < 1e-6
    total = sum(f["flow"] * f["exposure_cost"] for f in case["flows"])
    assert abs(total - case["total_cost"]) < 1e-6


def test_allocate_each_case_exact_flow_and_cost():
    edges = _allocation_edges()
    r = allocate_network(source="S", sink="T", nodes=["A", "B"],
                         edges=edges, required_flow=95)
    assert r["passed"] is True
    # 重审结论随响应带回
    assert r["audit"]["passed"] is True
    cases = r["allocations"]["cases"]
    # 情形 1 正常网络 + 每条可检修管段一条失效情形，编号连续
    assert [c["case_no"] for c in cases] == [1, 2, 3, 4, 5]
    normal = cases[0]
    assert normal["stage"] == "normal"
    _check_case(normal, edges, 95)
    # 正常网络全走低代价干线：95 经 A，总代价 95*(1+1)=190
    assert [f["flow"] for f in normal["flows"]] == [95, 95, 0, 0]
    assert normal["total_cost"] == 190
    for case, idx in zip(cases[1:], range(4)):
        assert case["stage"] == "single_failure"
        assert case["edge_index"] == idx and case["position"] == idx + 1
        _check_case(case, edges, 95, removed_index=idx)
    # E1（S→A）失效后只能走高代价干线：95*(5+5)=950
    assert cases[1]["total_cost"] == 950
    assert [f["flow"] for f in cases[1]["flows"]] == [0, 0, 95, 95]


def test_allocate_lexicographic_tie_break_by_input_order():
    """总代价并列时，按录入顺序流量序列字典序（前者优先取小）决胜。"""
    edges = [
        {"id": "E1", "from": "S", "to": "T", "capacity": 60, "maintainable": False, "exposure_cost": 0},
        {"id": "E2", "from": "S", "to": "T", "capacity": 60, "maintainable": False, "exposure_cost": 0},
    ]
    r = allocate_network(source="S", sink="T", nodes=[], edges=edges, required_flow=95)
    seq = [f["flow"] for f in r["allocations"]["cases"][0]["flows"]]
    # E1 容量 60 < 95，故 E1 最小可行流量为 35，E2 = 60
    assert seq == [35, 60]

    # 交换录入顺序后决胜结果随之交换，证明决胜确实按录入顺序
    swapped = [dict(edges[1], id="E1"), dict(edges[0], id="E2")]
    r2 = allocate_network(source="S", sink="T", nodes=[], edges=swapped, required_flow=95)
    seq2 = [f["flow"] for f in r2["allocations"]["cases"][0]["flows"]]
    assert seq2 == [35, 60]  # 永远是第一条录入管段尽量小


def test_allocate_tie_break_is_stable_and_unique():
    """更复杂零代价网络：决胜后序列唯一且可复核守恒。"""
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": False, "exposure_cost": 0},
        {"id": "E2", "from": "S", "to": "B", "capacity": 100, "maintainable": False, "exposure_cost": 0},
        {"id": "E3", "from": "A", "to": "T", "capacity": 60, "maintainable": False, "exposure_cost": 0},
        {"id": "E4", "from": "B", "to": "T", "capacity": 60, "maintainable": False, "exposure_cost": 0},
        {"id": "E5", "from": "A", "to": "B", "capacity": 100, "maintainable": False, "exposure_cost": 0},
    ]
    r = allocate_network(source="S", sink="T", nodes=["A", "B"],
                         edges=edges, required_flow=100)
    case = r["allocations"]["cases"][0]
    _check_case(case, edges, 100)
    # 先压 E1（S→A）：A→T 仅 60 且 A→B 可转运，E1 最小 40
    assert [f["flow"] for f in case["flows"]] == [40, 60, 40, 60, 0]


def test_allocate_blocked_when_audit_fails():
    """草稿不放行时不生成配流单，但仍返回本次重审的失败证据。"""
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True, "exposure_cost": 1},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True, "exposure_cost": 1},
        {"id": "E3", "from": "S", "to": "B", "capacity": 90, "maintainable": True, "exposure_cost": 5},
        {"id": "E4", "from": "B", "to": "T", "capacity": 90, "maintainable": True, "exposure_cost": 5},
    ]
    r = allocate_network(source="S", sink="T", nodes=["A", "B"],
                         edges=edges, required_flow=95)
    assert r["passed"] is False
    assert r["allocations"] is None
    assert r["audit"]["passed"] is False
    assert r["audit"]["failure"]["position"] == 1
    assert r["audit"]["failure"]["cut"]["capacity"] == 90


def test_allocate_failed_audit_does_not_require_costs():
    """不放行时即使代价未填写，也返回失败结论而不是 400。"""
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
        {"id": "E3", "from": "S", "to": "B", "capacity": 90, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 90, "maintainable": True},
    ]
    r = allocate_network(source="S", sink="T", nodes=["A", "B"],
                         edges=edges, required_flow=95)
    assert r["passed"] is False and r["allocations"] is None


def test_allocate_revalidates_draft():
    """配流提交必须重新审计：网络本身非法（幽灵节点）直接 400。"""
    edges = [
        {"id": "E1", "from": "S", "to": "幽灵", "capacity": 10, "maintainable": True, "exposure_cost": 1},
    ]
    with pytest.raises(NetworkValidationError) as exc:
        allocate_network(source="S", sink="T", nodes=[], edges=edges, required_flow=1)
    assert "未在节点中定义" in str(exc.value)


@pytest.mark.parametrize("bad", [-1, -0.01, 1.5, "abc", None, "", True])
def test_allocate_invalid_exposure_cost_rejected(bad):
    edges = [
        {"id": "E1", "from": "S", "to": "T", "capacity": 100, "maintainable": False, "exposure_cost": bad},
    ]
    with pytest.raises(NetworkValidationError):
        allocate_network(source="S", sink="T", nodes=[], edges=edges, required_flow=10)


@pytest.mark.parametrize("good", [0, 3, 100, 7.0, "8"])
def test_allocate_accepts_nonneg_integer_cost_forms(good):
    edges = [
        {"id": "E1", "from": "S", "to": "T", "capacity": 100, "maintainable": False, "exposure_cost": good},
    ]
    r = allocate_network(source="S", sink="T", nodes=[], edges=edges, required_flow=10)
    assert r["passed"] is True
    assert r["allocations"]["cases"][0]["flows"][0]["exposure_cost"] == int(float(good))


def test_allocate_fractional_required_flow():
    """要求量/容量允许小数（≤6 位），内部按整数单位精确配流。"""
    edges = [
        {"id": "E1", "from": "S", "to": "T", "capacity": 1, "maintainable": False, "exposure_cost": 2},
    ]
    r = allocate_network(source="S", sink="T", nodes=[], edges=edges, required_flow=0.5)
    case = r["allocations"]["cases"][0]
    assert case["flows"][0]["flow"] == 0.5
    assert case["total_cost"] == 1


def test_allocate_non_maintainable_edge_never_removed():
    edges = _allocation_edges()
    edges.append({"id": "E5", "from": "A", "to": "B", "capacity": 10,
                  "maintainable": False, "exposure_cost": 0})
    r = allocate_network(source="S", sink="T", nodes=["A", "B"],
                         edges=edges, required_flow=95)
    cases = r["allocations"]["cases"]
    assert len(cases) == 5  # 正常 + 4 条可检修；E5 不产生失效情形
    for case in cases:
        assert all(not f["removed"] for f in case["flows"] if f["id"] == "E5")
