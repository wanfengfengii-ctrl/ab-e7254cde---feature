"""业务 API 测试：健康检查、审计放行/失败证据、非法输入 400、静态页面、
低暴露配流单。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PASS_PAYLOAD = {
    "source": "S",
    "sink": "T",
    "required_flow": 95,
    "nodes": ["A", "B"],
    "edges": [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
        {"id": "E3", "from": "S", "to": "B", "capacity": 100, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 100, "maintainable": True},
    ],
}

# 与 PASS_PAYLOAD 同构，但带非负整数单位暴露代价（经 A 低、经 B 高）
ALLOC_PAYLOAD = {
    "source": "S",
    "sink": "T",
    "required_flow": 95,
    "nodes": ["A", "B"],
    "edges": [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True, "exposure_cost": 1},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True, "exposure_cost": 1},
        {"id": "E3", "from": "S", "to": "B", "capacity": 100, "maintainable": True, "exposure_cost": 5},
        {"id": "E4", "from": "B", "to": "T", "capacity": 100, "maintainable": True, "exposure_cost": 5},
    ],
}

FAIL_PAYLOAD = {
    "source": "S",
    "sink": "T",
    "required_flow": 95,
    "nodes": ["A", "B"],
    "edges": [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
        {"id": "E3", "from": "S", "to": "B", "capacity": 90, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 90, "maintainable": True},
    ],
}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_page_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "事故导排" in r.text


def test_audit_pass():
    r = client.post("/api/audit", json=PASS_PAYLOAD)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    assert body["normal"]["max_flow"] == 200
    assert len(body["scenarios"]) == 4
    assert all(s["meets"] for s in body["scenarios"])
    assert body["failure"] is None


def test_audit_fail_returns_first_edge_and_cut():
    r = client.post("/api/audit", json=FAIL_PAYLOAD)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    f = body["failure"]
    assert f["position"] == 1 and f["edge_id"] == "E1"
    assert f["max_flow"] == 90
    cut = f["cut"]
    assert cut["capacity"] == 90
    assert "S" in cut["source_side_nodes"]
    assert "T" in cut["sink_side_nodes"]
    assert len(cut["cut_edges"]) >= 1


def test_audit_invalid_node_reference_400():
    bad = dict(PASS_PAYLOAD)
    bad["edges"] = [{"from": "S", "to": "不存在", "capacity": 10, "maintainable": True}]
    r = client.post("/api/audit", json=bad)
    assert r.status_code == 400
    assert "未在节点中定义" in r.json()["error"]


def test_audit_invalid_capacity_400():
    bad = dict(PASS_PAYLOAD)
    bad["edges"] = [{"from": "S", "to": "T", "capacity": 0, "maintainable": True}]
    r = client.post("/api/audit", json=bad)
    assert r.status_code == 400
    assert "大于 0" in r.json()["error"]


def test_audit_invalid_direction_self_loop_400():
    bad = dict(PASS_PAYLOAD)
    bad["edges"] = [{"from": "S", "to": "S", "capacity": 10, "maintainable": True}]
    r = client.post("/api/audit", json=bad)
    assert r.status_code == 400
    assert "起点和终点不能相同" in r.json()["error"]


def test_audit_non_json_body_400():
    r = client.post("/api/audit", content=b"not-json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 低暴露配流单
# ---------------------------------------------------------------------------


def test_allocate_pass_returns_cases_and_costs():
    r = client.post("/api/allocate", json=ALLOC_PAYLOAD)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    # 服务端先按既有规则重审
    assert body["audit"]["passed"] is True
    assert body["audit"]["normal"]["max_flow"] == 200
    cases = body["allocations"]["cases"]
    assert [c["case_no"] for c in cases] == [1, 2, 3, 4, 5]
    normal = cases[0]
    assert normal["stage"] == "normal"
    assert [f["flow"] for f in normal["flows"]] == [95, 95, 0, 0]
    assert normal["total_cost"] == 190
    # 每个失效情形恰好排出 95，被移除管段流量固定为 0
    for i, case in enumerate(cases[1:]):
        assert case["stage"] == "single_failure"
        assert case["edge_index"] == i
        flows = case["flows"]
        assert flows[i]["removed"] is True and flows[i]["flow"] == 0
        assert sum(  # 源点净流出恰好等于要求量
            f["flow"] for f in flows if f["from"] == "S"
        ) - sum(f["flow"] for f in flows if f["to"] == "S") == 95
        assert case["required_flow"] == 95
    # E1 失效后全部走高代价干线
    assert cases[1]["total_cost"] == 950
    assert [f["flow"] for f in cases[1]["flows"]] == [0, 0, 95, 95]


def test_allocate_reaudits_and_blocks_when_not_passed():
    """草稿不放行：HTTP 仍 200，但 allocations 为 null，含首条失效管段与割集。"""
    payload = {
        "source": "S", "sink": "T", "required_flow": 95, "nodes": ["A", "B"],
        "edges": [
            {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True, "exposure_cost": 1},
            {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True, "exposure_cost": 1},
            {"id": "E3", "from": "S", "to": "B", "capacity": 90, "maintainable": True, "exposure_cost": 5},
            {"id": "E4", "from": "B", "to": "T", "capacity": 90, "maintainable": True, "exposure_cost": 5},
        ],
    }
    r = client.post("/api/allocate", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    assert body["allocations"] is None
    f = body["audit"]["failure"]
    assert f["position"] == 1 and f["max_flow"] == 90
    assert f["cut"]["capacity"] == 90


def test_allocate_invalid_cost_400():
    bad = {
        "source": "S", "sink": "T", "required_flow": 95, "nodes": ["A", "B"],
        "edges": [dict(e, exposure_cost=-1) for e in ALLOC_PAYLOAD["edges"]],
    }
    r = client.post("/api/allocate", json=bad)
    assert r.status_code == 400
    assert "暴露代价" in r.json()["error"]

    bad2 = {
        "source": "S", "sink": "T", "required_flow": 95, "nodes": ["A", "B"],
        "edges": [dict(e, exposure_cost=1.5) for e in ALLOC_PAYLOAD["edges"]],
    }
    r2 = client.post("/api/allocate", json=bad2)
    assert r2.status_code == 400
    assert "非负整数" in r2.json()["error"]


def test_allocate_invalid_draft_400():
    bad = dict(ALLOC_PAYLOAD)
    bad["edges"] = [{"id": "X", "from": "S", "to": "幽灵节点",
                     "capacity": 10, "maintainable": True, "exposure_cost": 1}]
    r = client.post("/api/allocate", json=bad)
    assert r.status_code == 400
    assert "未在节点中定义" in r.json()["error"]


def test_allocate_non_json_body_400():
    r = client.post("/api/allocate", content=b"not-json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
