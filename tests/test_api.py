"""关键操作测试：刊发/撤稿/重刊、首查与续页、快照语义、撤稿不阻塞计分。"""

import base64
import json
import os
import sys
import tempfile
import threading
import time

import pytest

_fd, _DB = tempfile.mkstemp(suffix=".sqlite3")
os.close(_fd)
os.unlink(_DB)  # 交给应用重建
os.environ["DB_PATH"] = _DB
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """每个测试一个独立的 SQLite 库。"""
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "test.sqlite3"))
    main._init_db()
    yield


# 语料：7 篇分词转写稿。查询词 ["抗战", "北平"] 时的预期榜序见 test_walkthrough。
DOCS = {
    "MS-001": ["抗战", "北平", "胡同", "童年", "粮票"],
    "MS-002": ["抗战", "上海", "弄堂", "工厂", "夜校"],
    "MS-003": ["北平", "茶馆", "曲艺", "抗战", "胜利", "广播"],
    "MS-004": ["知青", "下乡", "北大荒", "麦田"],
    "MS-005": ["抗战", "西南联大", "昆明", "北平", "师生"],
    "MS-006": ["北平", "城墙", "骆驼", "运输队"],
    "MS-007": ["抗战", "伤兵", "医院", "护士"],
}
TERMS = ["抗战", "北平"]


def _publish(client, doc_id, tokens):
    return client.post("/manuscripts", json={"doc_id": doc_id, "tokens": tokens})


def test_publish_withdraw_republish(client, fresh_db):
    """在刊稿号拒绝重复刊发；撤稿后可同号重刊，版本号递增。"""
    r = _publish(client, "T-100", ["抗战", "重庆", "防空洞"])
    assert r.status_code == 201, r.text
    v1 = r.json()["version"]

    r = _publish(client, "T-100", ["抗战", "重庆"])
    assert r.status_code == 409  # 在刊稿号重复刊发被拒

    r = client.post("/manuscripts/T-100/withdraw")
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"

    r = client.post("/manuscripts/T-100/withdraw")
    assert r.status_code == 404  # 已撤稿，无在刊版本

    r = _publish(client, "T-100", ["抗战", "重庆", "重逢"])
    assert r.status_code == 201  # 撤稿后可同号重刊
    assert r.json()["version"] > v1  # 新版本序号

    r = client.post("/manuscripts/NO-SUCH/withdraw")
    assert r.status_code == 404


def test_walkthrough(client, fresh_db):
    """首查→撤稿→续页跳过并补足→重刊→旧游标不可见、新检索可见。"""
    for doc_id, tokens in DOCS.items():
        r = _publish(client, doc_id, tokens)
        assert r.status_code == 201, r.text

    # 重复刊发在刊稿号被拒
    assert _publish(client, "MS-001", ["抗战"]).status_code == 409

    # 全量探针：记录首查时刻的完整榜序与分数
    probe = client.post("/search", json={"terms": TERMS, "size": 100}).json()
    assert probe["next_cursor"] is None  # 5 篇命中 <= 页长，无续页
    order = [h["doc_id"] for h in probe["hits"]]
    assert order == ["MS-001", "MS-005", "MS-003", "MS-006", "MS-007", "MS-002"]
    # MS-001 与 MS-005 同分，按稿号升序
    assert probe["hits"][0]["score"] == probe["hits"][1]["score"]
    # 命中至少一个查询词：MS-004 不含查询词，不在榜上
    assert "MS-004" not in order
    scores = {h["doc_id"]: h["score"] for h in probe["hits"]}

    # query 字符串形式与 terms 等价
    probe2 = client.post("/search", json={"query": "抗战 北平", "size": 100}).json()
    assert [h["doc_id"] for h in probe2["hits"]] == order

    # 首查：页长 2
    first = client.post("/search", json={"terms": TERMS, "size": 2}).json()
    assert [h["doc_id"] for h in first["hits"]] == ["MS-001", "MS-005"]
    assert first["hits"][0]["score"] == scores["MS-001"]
    assert first["next_cursor"]

    # 首查与续页之间：馆方撤掉 MS-003（原本第 3 名）
    r = client.post("/manuscripts/MS-003/withdraw")
    assert r.status_code == 200

    # 续页：跳过已撤的 MS-003，从后续命中补足当页
    page2 = client.post("/search/next", json={"cursor": first["next_cursor"]}).json()
    assert [h["doc_id"] for h in page2["hits"]] == ["MS-006", "MS-007"]
    # 沿用首查的分数（与探针逐位一致）
    assert page2["hits"][0]["score"] == scores["MS-006"]
    assert page2["hits"][1]["score"] == scores["MS-007"]
    assert page2["next_cursor"]

    # 同号重刊 MS-003：新版本序号
    r = _publish(client, "MS-003", ["北平", "抗战", "重逢", "口述", "整理"])
    assert r.status_code == 201
    new_version = r.json()["version"]
    assert new_version > 7

    # 旧游标继续翻页：重刊版本不进入首查前已开的检索
    page3 = client.post("/search/next", json={"cursor": page2["next_cursor"]}).json()
    assert [h["doc_id"] for h in page3["hits"]] == ["MS-002"]
    assert "MS-003" not in [h["doc_id"] for h in page3["hits"]]
    assert page3["next_cursor"] is None

    # 重刊后新开的检索：能看到新版本
    fresh = client.post("/search", json={"terms": TERMS, "size": 100}).json()
    by_id = {h["doc_id"]: h for h in fresh["hits"]}
    assert by_id["MS-003"]["version"] == new_version


def test_continue_size_override_and_cursor_reuse(client, fresh_db):
    """续页可临时改页长；游标可重复使用（幂等）。"""
    for i in range(1, 5):
        _publish(client, f"S-{i}", ["抗战", f"词{i}"])
    first = client.post("/search", json={"terms": ["抗战"], "size": 2}).json()
    assert len(first["hits"]) == 2
    # 同一游标用两次，结果一致
    p2a = client.post("/search/next", json={"cursor": first["next_cursor"], "size": 1}).json()
    p2b = client.post("/search/next", json={"cursor": first["next_cursor"], "size": 1}).json()
    assert p2a["hits"] == p2b["hits"] and len(p2a["hits"]) == 1
    # 坏游标 / 未知游标
    assert client.post("/search/next", json={"cursor": "not-a-cursor"}).status_code == 400
    ghost = base64.urlsafe_b64encode(
        json.dumps({"sid": "no-such-search", "after": [1.0, "A"]}).encode()
    ).decode()
    assert client.post("/search/next", json={"cursor": ghost}).status_code == 404


def test_withdraw_does_not_wait_for_scoring(client, fresh_db):
    """撤稿提交不必等正在计分的请求结束；计分请求仍按首查时刻快照返回。"""
    _publish(client, "NB-200", ["抗战", "南京", "渡口"])
    _publish(client, "NB-201", ["抗战", "武汉", "轮渡"])

    started = threading.Event()
    outcome = {}

    def slow_search():
        started.set()
        r = client.post("/search", json={
            "terms": ["抗战"], "size": 100, "debug_delay_seconds": 3.0,
        })
        outcome["search"] = r

    t = threading.Thread(target=slow_search)
    t.start()
    assert started.wait(timeout=5)
    time.sleep(0.3)  # 确认对方进入计分阶段

    t0 = time.monotonic()
    r = client.post("/manuscripts/NB-200/withdraw")
    elapsed = time.monotonic() - t0
    t.join(timeout=10)

    assert r.status_code == 200
    assert elapsed < 2.0, f"撤稿耗时 {elapsed:.2f}s，被计分请求阻塞"
    assert outcome["search"].status_code == 200
    # 慢检索取的是撤稿前的快照，NB-200 仍在其结果中
    assert "NB-200" in [h["doc_id"] for h in outcome["search"].json()["hits"]]


# ------------------------------------------------------------------ 搭配核查

# 语料：关键词“矿井”出现在 CL-001..CL-004 四篇。
# 词距 1：风机/巷道各 2 篇同现；词距 2：巷道 3 篇；词距 3：煤车 2 篇。
COLLOC_DOCS = {
    "CL-001": ["矿井", "巷道", "工牌", "老周"],
    "CL-002": ["早班", "矿井", "巷道", "灯房"],
    "CL-003": ["矿井", "风机", "巷道", "煤车"],
    "CL-004": ["班长", "矿井", "风机", "煤车"],
    "CL-005": ["巷道", "风机", "井口"],
    "CL-006": ["巷道", "学徒", "夜校"],
}


def _seed(client, docs):
    for doc_id, tokens in docs.items():
        r = _publish(client, doc_id, tokens)
        assert r.status_code == 201, r.text


def _colloc(client, **body):
    return client.post("/collocations", json=body)


def test_collocation_score_and_order(client, fresh_db):
    """分数公式、最稿距出处、同分字典序、limit 生效。"""
    _seed(client, COLLOC_DOCS)

    r = _colloc(client, term="矿井", max_distance=2, limit=20)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["published_manuscripts"] == 6
    assert body["keyword_doc_count"] == 4

    # 巷道：3 篇同现 / 4 篇在刊（巷道在 CL-001..003,005,006 共 5 篇）
    # 分数 = 3 × 6 ÷ (4 × 5) = 0.9
    # 风机：2 篇同现（CL-003, CL-004） / 4 篇在刊（CL-003..005 共 3 篇）
    # 分数 = 2 × 6 ÷ (4 × 3) = 1.0
    assert [x["collocate"] for x in body["results"]] == ["风机", "巷道"]
    fengji, xiangdao = body["results"]
    assert fengji["score"] == pytest.approx(1.0)
    assert fengji["cooccurring_docs"] == 2
    assert fengji["evidence"] == {
        "doc_id": "CL-003", "version": 3,
        "keyword_position": 0, "collocate_position": 1, "distance": 1,
    }
    assert xiangdao["score"] == pytest.approx(0.9)
    assert xiangdao["cooccurring_docs"] == 3
    # 词距最近的一处：CL-001 矿井@0 巷道@1
    assert xiangdao["evidence"]["doc_id"] == "CL-001"
    assert xiangdao["evidence"]["keyword_position"] == 0
    assert xiangdao["evidence"]["collocate_position"] == 1
    assert xiangdao["evidence"]["distance"] == 1

    # limit 截断
    one = _colloc(client, term="矿井", max_distance=2, limit=1).json()
    assert [x["collocate"] for x in one["results"]] == ["风机"]

    # 只有 1 篇同现的词不上榜：词距 2 时 工牌/老周/早班/灯房/煤车 均不足 2 篇
    assert all(x["cooccurring_docs"] >= 2 for x in body["results"])


def test_collocation_distance_boundary(client, fresh_db):
    """词距严格按位置差；换词距无需重建索引。"""
    _seed(client, COLLOC_DOCS)
    # 煤车 在 CL-003(矿井@0,煤车@3 差 3) 与 CL-004(矿井@1,煤车@3 差 2)
    d2 = _colloc(client, term="矿井", max_distance=2, limit=20).json()
    assert "煤车" not in [x["collocate"] for x in d2["results"]]
    d3 = _colloc(client, term="矿井", max_distance=3, limit=20).json()
    by = {x["collocate"]: x for x in d3["results"]}
    assert by["煤车"]["cooccurring_docs"] == 2
    assert by["煤车"]["score"] == pytest.approx(2 * 6 / (4 * 2))
    # 煤车在刊稿数 2（CL-003、CL-004）
    assert by["煤车"]["evidence"]["doc_id"] == "CL-004"
    assert by["煤车"]["evidence"]["distance"] == 2

    # 词距 1：巷道只有 CL-001、CL-002 两篇（CL-003 矿井@0 巷道@2 超距）
    d1 = _colloc(client, term="矿井", max_distance=1, limit=20).json()
    assert {x["collocate"]: x["cooccurring_docs"] for x in d1["results"]} == {
        "风机": 2, "巷道": 2,
    }
    # 索引未被重建，同一份语料可反复换词距查询
    check = client.get("/collocations/upgrade-check").json()
    assert check["consistent"] is True


def test_collocation_one_per_manuscript_and_evidence(client, fresh_db):
    """一稿只计一次；出处取词距最近的一处，位置从 0 起。"""
    _seed(client, {
        "MC-001": ["矿井", "矿灯", "矿井", "巷道", "矿井"],
        "MC-002": ["矿井", "巷道", "矿灯"],
        "MC-003": ["矿井", "巷道"],
    })
    body = _colloc(client, term="矿井", max_distance=2, limit=20).json()
    by = {x["collocate"]: x for x in body["results"]}
    # 巷道在 3 篇同现，但 MC-001 中只计 1 次
    assert by["巷道"]["cooccurring_docs"] == 3
    ev = by["巷道"]["evidence"]
    assert ev["doc_id"] == "MC-001"
    # MC-001: 矿井@2 与 巷道@3 相距 1，比 矿井@0 更近
    assert (ev["keyword_position"], ev["collocate_position"]) == (2, 3)
    assert ev["distance"] == 1
    # 矿灯只与 MC-001/MC-002 同现，但需要 2 篇 => 可上榜
    assert by["矿灯"]["cooccurring_docs"] == 2


def test_collocation_min_two_manuscripts(client, fresh_db):
    """只在单篇同现的词不上榜。"""
    _seed(client, {
        "S-1": ["矿井", "独词"],
        "S-2": ["矿井"],
    })
    body = _colloc(client, term="矿井", max_distance=5, limit=20).json()
    assert body["results"] == []


def test_collocation_withdraw_changes_ranking(client, fresh_db):
    """撤掉一篇不含关键词的在刊稿：只改总稿数，分数与榜序随之变化。"""
    _seed(client, COLLOC_DOCS)
    before = _colloc(client, term="矿井", max_distance=2, limit=20).json()
    assert [x["collocate"] for x in before["results"]] == ["风机", "巷道"]
    assert before["results"][0]["score"] == pytest.approx(1.0)

    # CL-006 不含“矿井”，撤稿后 N=5、巷道在刊稿数 4
    assert client.post("/manuscripts/CL-006/withdraw").status_code == 200
    after = _colloc(client, term="矿井", max_distance=2, limit=20).json()
    assert after["published_manuscripts"] == 5
    assert after["keyword_doc_count"] == 4
    by = {x["collocate"]: x for x in after["results"]}
    # 巷道 = 3 × 5 / (4 × 4) = 0.9375 > 风机 = 2 × 5 / (4 × 3) ≈ 0.8333
    assert by["巷道"]["score"] == pytest.approx(3 * 5 / (4 * 4))
    assert by["风机"]["score"] == pytest.approx(2 * 5 / (4 * 3))
    assert [x["collocate"] for x in after["results"]] == ["巷道", "风机"]

    # 撤稿即时反映：统计与稿件状态同事务生效
    check = client.get("/collocations/upgrade-check").json()
    assert check["consistent"] is True


def test_collocation_validation(client, fresh_db):
    """参数边界与空关键词；未命中关键词返回空榜。"""
    _seed(client, COLLOC_DOCS)
    assert _colloc(client, term="矿井", max_distance=0, limit=5).status_code == 422
    assert _colloc(client, term="矿井", max_distance=31, limit=5).status_code == 422
    assert _colloc(client, term="矿井", max_distance=5, limit=0).status_code == 422
    assert _colloc(client, term="矿井", max_distance=5, limit=21).status_code == 422
    assert _colloc(client, term="   ", max_distance=5, limit=5).status_code == 422

    body = _colloc(client, term="不存在的词", max_distance=5, limit=5).json()
    assert body["results"] == []
    assert body["keyword_doc_count"] == 0


def test_collocation_only_reads_keyword_manuscripts(client, fresh_db):
    """查询只读含关键词的稿件分词；不含关键词的新稿只改变汇总总稿数。"""
    _seed(client, COLLOC_DOCS)
    before = _colloc(client, term="矿井", max_distance=2, limit=20).json()
    xiangdao_ev = {x["collocate"]: x for x in before["results"]}["巷道"]["evidence"]

    # 新增两篇不含关键词的稿件（Y-1 含“风机”，但不在关键词稿件里）
    _seed(client, {
        "Y-1": ["风机", "黑板"],
        "Y-2": ["风机", "巷道"],
    })
    after = _colloc(client, term="矿井", max_distance=2, limit=20).json()
    assert after["published_manuscripts"] == 8
    assert after["keyword_doc_count"] == 4  # 关键词稿件数不变
    by = {x["collocate"]: x for x in after["results"]}
    # 同现只来自含关键词的稿件：风机仍 2 篇、巷道仍 3 篇
    assert by["风机"]["cooccurring_docs"] == 2
    assert by["巷道"]["cooccurring_docs"] == 3
    assert by["巷道"]["evidence"] == xiangdao_ev
    # 但两者在刊稿数都变了：风机 5 篇（CL-003..005 + Y-1/Y-2），
    # 巷道 6 篇（CL-001/002/003/005/006 + Y-2）；分数按新总稿数 8 重算
    assert by["风机"]["score"] == pytest.approx(2 * 8 / (4 * 5))
    assert by["巷道"]["score"] == pytest.approx(3 * 8 / (4 * 6))


def test_collocation_upgrade_old_database(client, fresh_db):
    """旧库（无词级索引）升级后可查，且迁移可重复执行。"""
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(main.DB_PATH)
    conn.row_factory = _sqlite3.Row
    conn.execute("DROP TABLE IF EXISTS term_postings")
    conn.execute("DROP TABLE IF EXISTS term_stats")
    for doc_id, tokens in COLLOC_DOCS.items():
        tf = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        conn.execute(
            "INSERT INTO manuscripts (doc_id, tokens, tf, length, status, published_at)"
            " VALUES (?,?,?,?, 'published', '2026-01-01')",
            (doc_id, json.dumps(tokens, ensure_ascii=False),
             json.dumps(tf, ensure_ascii=False), len(tokens)),
        )
    conn.commit()
    conn.close()

    # 重新初始化（模拟服务升级启动）：索引按已刊发稿自动补齐
    main._init_db()
    check = client.get("/collocations/upgrade-check").json()
    assert check["consistent"] is True
    body = _colloc(client, term="矿井", max_distance=2, limit=20).json()
    assert [x["collocate"] for x in body["results"]] == ["风机", "巷道"]
    assert body["keyword_doc_count"] == 4

    # 再次初始化不改变结果（幂等）
    main._init_db()
    assert _colloc(client, term="矿井", max_distance=2, limit=20).json() == body


def test_collocation_snapshot_during_withdraw(client, fresh_db):
    """一次核查读同一份已提交语料；核查期间撤稿不阻塞、不影响本次结果。"""
    _seed(client, COLLOC_DOCS)
    expected = _colloc(client, term="矿井", max_distance=2, limit=20).json()

    started = threading.Event()
    outcome = {}

    def slow_colloc():
        started.set()
        outcome["r"] = _colloc(
            client, term="矿井", max_distance=2, limit=20, debug_delay_seconds=2.0,
        )

    t = threading.Thread(target=slow_colloc)
    t.start()
    assert started.wait(timeout=5)
    time.sleep(0.3)
    t0 = time.monotonic()
    r = client.post("/manuscripts/CL-006/withdraw")
    elapsed = time.monotonic() - t0
    t.join(timeout=10)

    assert r.status_code == 200
    assert elapsed < 1.5
    # 本次核查用的是撤稿前的快照
    assert outcome["r"].json() == expected
    # 下一次核查看到撤稿后的语料
    assert _colloc(client, term="矿井", max_distance=2, limit=20).json()[
        "published_manuscripts"
    ] == 5


def test_collocation_state_and_stats_atomic(client, fresh_db):
    """刊发/撤稿失败时，稿件状态与词级统计都保持原状。"""
    _seed(client, {"A-1": ["矿井", "巷道"], "A-2": ["矿井", "巷道"]})
    before = _colloc(client, term="矿井", max_distance=2, limit=20).json()
    check_before = client.get("/collocations/upgrade-check").json()

    # 在刊稿号重复刊发：409，统计不重复累加
    assert _publish(client, "A-1", ["矿井", "风机"]).status_code == 409
    assert client.get("/collocations/upgrade-check").json() == check_before
    assert _colloc(client, term="矿井", max_distance=2, limit=20).json() == before

    # 撤不存在/已撤稿的稿号：404，统计不动
    assert client.post("/manuscripts/NO-SUCH/withdraw").status_code == 404
    assert client.post("/manuscripts/A-1/withdraw").status_code == 200
    after_withdraw = client.get("/collocations/upgrade-check").json()
    assert client.post("/manuscripts/A-1/withdraw").status_code == 404
    assert client.get("/collocations/upgrade-check").json() == after_withdraw


def test_collocation_withdraw_and_republish_updates_stats(client, fresh_db):
    """撤稿移除旧词统计；同号重刊按新分词重新计入，出处版本为新版本。"""
    _seed(client, {
        "R-1": ["矿井", "巷道"],
        "R-2": ["矿井", "巷道"],
        "R-3": ["矿井", "风机"],
        "R-4": ["矿井", "风机"],
    })
    body = _colloc(client, term="矿井", max_distance=2, limit=20).json()
    assert {x["collocate"]: x["cooccurring_docs"] for x in body["results"]} == {
        "巷道": 2, "风机": 2,
    }

    # 撤 R-1 后重刊为只含“风机”的稿件：巷道同现降到 1 篇，不再上榜
    assert client.post("/manuscripts/R-1/withdraw").status_code == 200
    r = _publish(client, "R-1", ["矿井", "风机", "新词"])
    assert r.status_code == 201
    new_version = r.json()["version"]

    after = _colloc(client, term="矿井", max_distance=2, limit=20).json()
    by = {x["collocate"]: x for x in after["results"]}
    # 巷道只剩 R-2 一篇，新词只在 R-1 一篇，均不足两篇同现
    assert set(by) == {"风机"}
    assert by["风机"]["cooccurring_docs"] == 3  # R-1(重刊)+R-3+R-4
    assert after["keyword_doc_count"] == 4
    check = client.get("/collocations/upgrade-check").json()
    assert check["consistent"] is True
    # 出处取词距最近的 R-1，且版本为重刊后的在刊版本
    assert by["风机"]["evidence"]["doc_id"] == "R-1"
    assert by["风机"]["evidence"]["version"] == new_version


def test_collocation_tied_scores_sorted_lexicographically(client, fresh_db):
    """同分时按词的字典序（Unicode 码位）升序。"""
    # “风机”与“巷道”同现稿数相同、在刊稿数相同 => 分数相等
    _seed(client, {
        "T-1": ["矿井", "风机"],
        "T-2": ["矿井", "风机"],
        "T-3": ["矿井", "巷道"],
        "T-4": ["矿井", "巷道"],
        "T-5": ["矿井"],
    })
    body = _colloc(client, term="矿井", max_distance=1, limit=20).json()
    results = body["results"]
    assert [x["collocate"] for x in results] == sorted(x["collocate"] for x in results)
    assert results[0]["score"] == results[1]["score"]
    # “巷道”(U+5DF7) 码位小于“风机”(U+98CE)
    assert [x["collocate"] for x in results] == ["巷道", "风机"]
