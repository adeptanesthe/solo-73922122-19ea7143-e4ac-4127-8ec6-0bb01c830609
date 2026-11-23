# 口述史馆检索 API

FastAPI + SQLite 实现的检索服务：编辑按稿号刊发/撤稿分词后的转写稿，读者用查询词做
BM25 检索并凭游标翻页；撤稿即时生效，续页自动跳过被撤版本；同号重刊只进入重刊后
新开的检索。另提供**搭配核查**：给定关键词与最大词位置差，列出附近常同现的词、
共现稿数、关联分数与最近一处出处，供馆员编口述史地名索引时参考。

## 目录结构

```
app/main.py            # 服务全部实现（FastAPI + sqlite3 标准库）
app/requirements.txt   # 运行依赖
tests/test_api.py      # 关键操作测试（pytest + TestClient）
Dockerfile             # 服务镜像
compose.yaml           # Docker Compose 启动
demo.sh                # 端到端演示脚本（撤稿/重刊/不阻塞计分/搭配核查）
```

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务监听 http://127.0.0.1:8000 ，SQLite 数据在命名卷 search_data 中
# 重新演示前如需清空数据：docker compose down -v
```

### 本地开发

```bash
pip install -r app/requirements.txt
cd app && uvicorn main:app --host 127.0.0.1 --port 8000
```

### 运行演示与测试

```bash
./demo.sh                          # 对 127.0.0.1:8000 跑完整场景（需 jq）
python3 -m pytest tests/ -v        # 自动化测试
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/manuscripts` | 刊发分词后的转写稿；在刊稿号重复刊发返回 409 |
| POST | `/manuscripts/{doc_id}/withdraw` | 撤稿（立即生效）；无在刊版本返回 404 |
| GET  | `/manuscripts` | 列出全部版本（含已撤稿），便于观察版本演变 |
| POST | `/search` | 首查：BM25 计分，返回第一页与 `next_cursor` |
| POST | `/search/next` | 凭游标续页，可临时改页长 |
| POST | `/collocations` | 搭配核查：关键词附近常同现的词及分数 |
| GET  | `/collocations/upgrade-check` | 只读校验词级索引与在刊稿一致（升级/巡检用） |
| GET  | `/healthz` | 健康检查 |

### 请求示例

```bash
# 刊发
curl -X POST localhost:8000/manuscripts -H 'Content-Type: application/json' \
  -d '{"doc_id":"MS-001","tokens":["抗战","北平","胡同","童年","粮票"]}'

# 首查：查询词 + 页长（terms 数组，或用 "query":"抗战 北平"）
curl -X POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"terms":["抗战","北平"],"size":2}'

# 续页
curl -X POST localhost:8000/search/next -H 'Content-Type: application/json' \
  -d '{"cursor":"<上页返回的 next_cursor>"}'

# 搭配核查：关键词 + 最大词位置差(1~30) + 返回条数(1~20)
curl -X POST localhost:8000/collocations -H 'Content-Type: application/json' \
  -d '{"term":"矿井","max_distance":2,"limit":10}'

# 撤稿 / 同号重刊
curl -X POST localhost:8000/manuscripts/MS-001/withdraw
curl -X POST localhost:8000/manuscripts -H 'Content-Type: application/json' \
  -d '{"doc_id":"MS-001","tokens":["北平","抗战","重逢","口述","整理"]}'
```

搭配核查返回示例：

```json
{
  "keyword": "矿井",
  "max_distance": 2,
  "limit": 10,
  "published_manuscripts": 12,
  "keyword_doc_count": 4,
  "results": [
    {
      "collocate": "风机",
      "score": 2.0,
      "cooccurring_docs": 2,
      "evidence": {
        "doc_id": "CO-003",
        "version": 11,
        "keyword_position": 0,
        "collocate_position": 1,
        "distance": 1
      }
    }
  ]
}
```

`demo.sh` 串起完整场景：刊发 7 篇 → 重复刊发被拒(409) → 首查(页长 2) →
撤掉第 3 名 → 续页跳过被撤版本并补足当页 → 同号重刊 → 旧游标看不到重刊版、
新检索能看到 → 撤稿不等待慢速计分请求 → 搭配核查改词距不重建索引 →
撤掉一篇不含关键词的稿使榜序翻转 → 慢核查期间撤稿不被阻塞。

## 关键设计

### 版本与刊发语义

- `manuscripts.version` 为自增版本序号，单调递增，作为全局"时间戳"。
- 部分唯一索引 `UNIQUE(doc_id) WHERE status='published'` 在数据库层保证同一稿号
  任一时刻至多一个在刊版本：在刊时重复刊发被 409 拒绝；撤稿后同号重刊插入新行、
  获得新版本号。

### 首查与游标

- 首查在**单个读事务**内读取一致快照：当前全部在刊稿 + 版本高水位
  `snapshot_seq`，据此计算 BM25（Okapi，k1=1.2，b=0.75，idf 取
  `ln(1+(N-df+0.5)/(df+0.5))` 保证为正）。命中至少一个查询词的稿件按
  **分数降序、同分按稿号升序**返回。
- 服务端为每个检索只登记**查询词、页长、语料统计（N、avgdl、各词 df）**
  （`searches` 表，O(查询词数)），**不保存整张榜单**；翻页位置
  `(上一页末条分数, 稿号)` 放在客户端持有的不透明游标里（base64 JSON）。
- 续页时取出冻结的语料统计，对 `version <= snapshot_seq` 且**当前仍在刊**的版本
  重算分数——相同输入下浮点结果逐位一致，因此分数与顺序与首查相同；再用键集
  分页取游标之后的 `size` 条。被撤版本在过滤时被跳过，后续命中自然上移补足当页；
  重刊版本 `version > snapshot_seq`，不会进入旧检索，只出现在重刊后新开的检索中。
- 游标可重复使用（幂等），续页可临时指定不同页长。

### 撤稿不阻塞计分

- SQLite 开 **WAL**：读（计分）与写（撤稿）互不等待；每请求独立连接，写事务
  只做一条 UPDATE/INSERT，毫秒级释放写锁；应用层没有任何全局锁。
- 同步视图函数由 Starlette 线程池并发执行，慢速计分请求不占着事件循环。
- `/search` 的 `debug_delay_seconds` 是**测试钩子**（默认 0），人为拉长计分耗时，
  用于演示/验证撤稿不必等计分结束（见 `demo.sh` 第 9 步与
  `tests/test_api.py::test_withdraw_does_not_wait_for_scoring`）。
- 同理 `/collocations` 的 `debug_delay_seconds` 在快照读取后延迟，用于验证核查期间
  可正常刊撤（`demo.sh` 第 12 步）。

### 搭配核查

- **同现定义**：关键词与另一词在同一篇在刊稿中的位置差 `|p-q| <= max_distance`
  即算同现；位置从 0 起。同一稿对同一词只计 1 次同现，不按出现次数累加。
- **打分**：`分数 = 同现稿数 × 在刊稿总数 N ÷ (关键词在刊稿数 × 同现词在刊稿数)`。
  只给**至少两篇在刊稿**同现的其他词打分。结果按分数降序、同分按词的字典序，
  取 `limit` 条。
- **出处**：每项附该词距最近的一处：
  `doc_id`、`version`、`keyword_position`、`collocate_position`（均从 0 起）
  及 `distance`。同一稿内多处同现时取距离最小者，同距取靠前的关键词位置；
  跨稿取距离最小，同距按稿号。保证确定性。
- **读取范围**：一次核查只读取含关键词的在刊稿件的 `tokens` 做窗口扫描；
  其他词的“在刊稿数”只查 `term_stats` 汇总表，不读其稿件分词。
- **索引与词距解耦**：`term_postings(term, doc_id, version)` 倒排表 + 
  `term_stats(term, doc_count)` 汇总表在**刊发/撤稿时同一事务**增量维护
  （撤稿 -1、重刊按新分词 +1），因此换 `max_distance` 无需重建任何索引；
  一次查询的代价是 O(含关键词稿件数 × 窗口)，与全库稿件量无关。
- **快照与撤稿**：一次核查在单个读事务内取一致快照（含关键词稿件、N、各词
  在刊稿数）；计算期间发生的刊发/撤稿不改变本次结果，也不被阻塞。
  状态与统计同事务提交：失败则两者都保持原状。
- **旧库升级**：启动时若发现倒排/汇总表不存在或迁移标记缺失，按当前在刊稿
  整库重建一次并写入 `meta.term_index_version`；之后启动只做 O(1) 标记检查。
  升级后无需重建即可核查，`GET /collocations/upgrade-check` 可核对统计一致性。

## 实测结果（交付前验证）

- `python3 -m pytest tests/ -q`：**16 passed**
  - 原有 4 项（刊发/撤稿/重刊、BM25 走查、续页、撤稿不阻塞计分）保持通过；
  - 搭配核查 12 项：
    - 分数公式手算核对（风机 1.0、巷道 0.9），降序排列，`limit` 截断；
    - **同分按词的字典序**（巷道 U+5DF7 排在风机 U+98CE 前）；
    - 词距边界（1/2/3 三层结果不同）且不重建索引；
    - 一稿对同词只计一次，出处取词距最近的 `(2,3)`；
    - 只在单篇同现的词不上榜；
    - **撤掉不含关键词的在刊稿后榜序翻转**（巷道上位）；
    - 参数边界 422（词距 0/31、条数 0/21、空白关键词），未命中关键词空榜；
    - 查询只读含关键词稿件：新增不含关键词的稿只改 N 与各词在刊稿数；
    - 旧库（无词级索引）升级后可查、重复初始化幂等；
    - 慢核查期间撤稿 60ms 内返回，本次结果仍为撤稿前快照；
    - 失败保留原状：重复刊发 409、重复撤稿 404 均不改变统计；
    - 撤稿 + 同号重刊后旧词统计移除、新分词重新计入，出处版本为新版本。
- `./demo.sh` 对本地 uvicorn 实例全流程跑通（13 步），关键输出：
  - 首查页 1：`MS-001, MS-005`（同分 0.9271）；撤 MS-003 后续页 `MS-006, MS-007`；
    重刊 MS-003 → `version=8`；
  - 搭配核查词距 2：`风机 2.0` > `巷道 1.8`；词距 1：两者同现均 2 篇；
  - 撤掉不含关键词的 CO-006：`巷道 2.0625` > `风机 1.8333`，榜序翻转；
  - 3 秒慢核查期间撤稿 **60ms** 返回，慢核查仍按撤稿前快照（N=11）；
  - `upgrade-check`：`consistent=true`，43=43=43。
- 旧库升级实测：用升级前 schema 造库（含 4 篇、无词级索引），启动后
  `upgrade-check` 即 `consistent=true`，搭配核查返回 `巷道 0.8889`，二次启动结果不变。
- 规模抽测：2 万篇在刊稿的旧库首次升级重建约 **1.95 秒**，二次启动 **1.3 毫秒**
  （只查迁移标记）；含关键词 500 篇时单次核查约 **37 毫秒**。

## 剩余事项

- 本环境没有 Docker（`docker` 命令不存在），`Dockerfile`/`compose.yaml` 未实际构建过；
  镜像 CMD 与本地验证过的启动命令一致（`uvicorn main:app`），建议在有 Docker 的
  环境跑一次 `docker compose up --build` 复核。本机以同一 `DB_PATH` 环境变量在
  127.0.0.1 启动 uvicorn 实测了全部接口。
- 续页采用"冻结统计 + 重算"，单次续页代价 O(在刊稿数)，是"不为游标存榜单"约束下
  的直接实现；稿件量大时可在内存里建语料级倒排索引（仍不按游标存榜单）来加速。
- `searches` 表随首查数量单调增长，需要加过期清理（如按 `created_at` 定期删除，
  过期游标返回 404 的逻辑已就位）。
- 稿号按字典序排序，建议馆方使用等宽编号（如 `MS-001`）；如需数值序可改存整数列。
- 未做认证鉴权与限流；`debug_delay_seconds` 测试钩子上生产前建议移除或加开关。
- 分数一致性依赖同一服务版本内浮点重算的确定性；滚动升级计分公式期间，旧游标
  的分数可能与新公式不一致（可通过在游标中记录公式版本号来防御）。
- 搭配核查的倒排与汇总随刊发/撤稿增量维护；若运维直接改库导致统计漂移，
  `upgrade-check` 会报 `consistent=false`，需清空 `meta.term_index_version`
  后重启触发一次重建（尚未提供在线重建接口）。
- 搭配分数采用与 BM25 相同的朴素共现比值（无平滑），小语料下分数可能偏高；
  如馆方需要，可再加 Dunning G² 或 Dice 等显著性度量作为可选参数。
