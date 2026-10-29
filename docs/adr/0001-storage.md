# ADR 0001：存储选型与迁移策略

- **状态**：Accepted（P0 存储抽象已实施 2026-10-29，88 tests）
- **日期**：2026-10-29
- **决策**：L1–L3 保持 **Chroma + SQLite**，不引入 Milvus / MySQL；通过存储 Protocol 抽象保证后续可迁移
- **待办**：`StudyStore` Protocol 随学习库 schema 一并设计
- **相关**：[PLAN v4](../../PLAN.md) · [知识治理](../knowledge-governance.md) · [L1→L3 拆解](../mvp-l1-to-l3.md)

---

## 1. 背景

- 单用户、本地优先、长期使用的个人知识助手
- 目标层级：L1 检索 → L2 知识结构 → L3 学习闭环
- 数据治理要求：scope 隔离、过期/冲突、重复合并、embedding 迁移
- 当前实现：Chroma（子 chunk 向量）+ `documents.sqlite3`（父文档全文）

估算规模：

| 数据 | 量级 |
| --- | --- |
| 文档 | 1k–5k |
| 子 chunk / 向量 | 5 万–50 万 |
| 概念 | 1k–5k |
| 题目 | 1 万–5 万 |
| 作答记录 | 每年 1–2 万 |
| 并发 | 1 用户 + 后台任务 |

## 2. 决策

1. **L1–L3 继续使用 Chroma + SQLite**，不引入 Milvus / MySQL
2. 定义 `VectorStore`、`DocumentStore`、`StudyStore` 三个 Protocol，加工厂与契约测试
3. 把 `where` 过滤从"Chroma 语法"提升为**后端中立过滤模型**
4. 现在就把 embedding manifest、scope、时效等元数据纳入 schema
5. 设定明确的迁移触发条件与目标，达到阈值再评估

## 3. 理由

### 3.1 规模匹配

Chroma 在 100 万向量以内、单机场景是舒适区；SQLite 处理千万行级关系数据没有压力。当前规模距离两者的实际上限都有一个数量级以上的余量。

### 3.2 形态匹配

- 单用户、单机、本地优先 → 嵌入式数据库是最低运维方案
- 学习层是强关系型数据（attempts / learner_state / reviews）→ SQLite 天然合适
- 图查询目前只需 1–2 跳 → 递归 CTE 即可，不需要图数据库

### 3.3 换库不解决核心复杂度

| 问题 | 换库能否解决 |
| --- | --- |
| 向量库与关系库不一致 | ❌ 仍需要 `doc_events` outbox |
| embedding 换模型 | ❌ 仍需全量重建 + 双写 + 影子评估 |
| 知识过期 / 重复 / 冲突 | ❌ 属于治理逻辑，与数据库无关 |
| 抽取 / 出题质量 | ❌ 属于模型与流程问题 |

Milvus + MySQL 没有跨存储事务；把 outbox 搬到更重的组件上，只会增加运维面。

### 3.4 运维成本

完整 Milvus 依赖 etcd、MinIO 等组件；MySQL 需要独立服务、备份与升级。对个人本地工具是净负担。

## 4. 后果

**正面**

- 零额外服务，备份就是文件
- 所有派生数据可从源文件重建
- 迁移路径清晰，不会锁死在 Chroma/SQLite

**负面**

- SQLite 单写者模型，需要单写队列与短事务
- Chroma 多进程访问需要收敛到单写进程
- 需要自行实现过滤模型、契约测试与迁移工具

**接受的权衡**：在达到迁移阈值前，用抽象与测试换掉运维复杂度。

## 5. 迁移触发条件

命中 **2–3 条且优化无效** 时才评估迁移；单条命中先优化。

| 条件 | 阈值 |
| --- | --- |
| 向量规模 | > 100 万–200 万 |
| 文档 / 块规模 | 文档 > 5 万 或 chunk > 50 万 |
| 并发写入 | > 1–3 个并发写者 |
| 查询延迟 | 调优后 p95 > 300–500ms |
| 部署形态 | 多实例、远程访问、多设备同步 |
| 稳定性 | 持续 `database is locked` 或 Chroma 写入失败，优化后仍存在 |

## 6. 迁移目标

| 需求 | 目标 |
| --- | --- |
| 向量（单机 / 中小规模） | Qdrant（本地或 server）、LanceDB（嵌入式） |
| 向量（> 1000 万 / GPU / 分片） | Milvus |
| 关系库（高并发 / 服务化） | PostgreSQL |
| 关系 + 向量合一 | PostgreSQL + pgvector |
| 深图查询（3+ 跳） | Kùzu（嵌入式）或 Neo4j |

**不推荐**：MySQL 作为首选迁移目标（JSONB、FTS、pgvector、递归 CTE 均弱于 Postgres）；Milvus 用于当前规模属于过度设计。

---

## 7. 现状审计（2026-10-29）

### 7.1 已有抽象

| 项 | 位置 | 评价 |
| --- | --- | --- |
| `VectorStore` Protocol | `vectorstore/chroma_store.py:10-45` | ✅ 已有 add/query/delete/get/count/collections |
| 构造注入 | `kb.py:33-34,52-53` | ✅ 可传入自定义 store |
| 业务模型与存储解耦 | `models.py:47-110` | ✅ 使用 dataclass，不暴露 Chroma 类型 |
| 嵌入与存储解耦 | `ingestion/indexer.py:167-173` | ✅ store 只接收向量 |
| 返回值普通化 | `vectorstore/chroma_store.py:93-105` | ✅ 返回 dict，不返回 Chroma 对象 |
| SQL 集中在适配器 | `document_store/sqlite_store.py` | ✅ 外部无散落 SQL |

### 7.2 阻碍迁移的耦合

| # | 问题 | 位置 | 影响 | 优先级 |
| --- | --- | --- | --- | --- |
| 1 | `DocumentStore` 没有 Protocol，直接依赖具体类 | `kb.py:13,53`；`ingestion/indexer.py:15,49-50` | 换 Postgres 要改类型与导入 | **P0** |
| 2 | `where` 参数直接暴露 Chroma 过滤语法 | `api/schemas.py:14`；`kb.py:159,193`；`generation/pipeline.py:91`；`api/server.py:87,97` | 过滤语法成为公开契约，换后端即破坏 API | **P0** |
| 3 | 相似度换算硬编码 L2 | `vectorstore/chroma_store.py:102` | cosine / IP 后端会得到错误分数，阈值失效 | **P0** |
| 4 | chunk 元数据不含 embedding 模型 / manifest / chunker 版本 | `ingestion/indexer.py:155-165` | 无法判断向量属于哪个模型，迁移与回滚不可控 | **P0** |
| 5 | store 默认实现写死在门面 | `kb.py:52-53` | 无法按配置切换后端 | **P0** |
| 6 | `VectorStore` 无 `close()`；`KnowledgeBase.close()` 只关文档库 | `kb.py:219-220` | 后端资源无法释放 / flush | P1 |
| 7 | 元数据标量化是 Chroma 特例 | `ingestion/indexer.py:35-41,130` | 换后端后限制不适用或不足 | P1 |
| 8 | 无 `docs_meta` / `doc_events` / scope / 时效字段 | `document_store/sqlite_store.py:12-29`；`models.py:47-61` | 治理与级联无落点 | **P0**（治理） |
| 9 | 跨存储写入无事务 | `ingestion/indexer.py:173,190,199-200` | 向量成功、文档失败 → 孤儿向量；需 outbox | P1 |
| 10 | `update = delete + re-ingest` | `ingestion/indexer.py:206-219` | 更新会丢失治理元数据与关联 | P1 |
| 11 | 无关键词 / BM25 抽象 | — | 后续混合检索需新增接口 | P2 |
| 12 | `where` 区间过滤依赖标量日期格式 | `api/schemas.py:14` | Chroma 不支持嵌套与复杂类型 | P1 |
| 13 | `StudyStore` 尚不存在 | — | 新增学习库时容易重复"直接依赖具体类"的老路 | **P0** |

### 7.3 结论

向量侧已有基本抽象，**关系侧没有**；真正的迁移障碍不是数据库性能，而是：
1. 过滤语法泄露
2. 分数语义不统一
3. 缺少向量版本元数据
4. 关系库无接口
5. 跨存储一致性依赖 outbox

---

## 8. 接口化改造清单

### P0（存储抽象已完成 2026-10-29）

- [x] 新增 `document_store/base.py`：`DocumentStore` Protocol
- [x] 新增 `storage/filters.py`：后端中立过滤模型 + 编译接口
- [x] 新增 `storage/factory.py`：按配置创建 store
- [x] 统一 `VectorHit` 结构：`distance`、`score`（0–1，越大越相关）、`metric`
- [x] chunk 元数据写入 `embedding_manifest_id`、`embedding_model`、`chunker_version`
- [ ] `StudyStore` Protocol 与 schema 同步设计（延后，避免在数据模型确定前锁死接口）
- [x] 契约测试：`tests/test_storage_abstraction.py`（17 个）

### P1

- [ ] `docs_meta`、`doc_events`、`consumer_cursor` 与 outbox 接入
- [ ] `close()` / `flush()` 生命周期
- [ ] 元数据规范化移入适配器
- [ ] 更新流程改为"写新版本 + 事件"，不再简单删除重建
- [ ] 日期字段统一为 ISO 8601 字符串或 epoch 整数（便于区间过滤）

### P2

- [ ] `KeywordIndex` Protocol（FTS5 先行），支持混合检索
- [ ] 第二个向量适配器（Qdrant 或 LanceDB）作为可迁移性验证
- [ ] 第二个关系适配器（PostgreSQL）至少完成 schema 映射与契约测试

---

## 9. 接口草案

```python
# document_store/base.py
class DocumentStore(Protocol):
    def upsert(self, doc: Document) -> None: ...
    def get(self, doc_id: str) -> Document | None: ...
    def get_by_hash(self, content_hash: str) -> Document | None: ...
    def list(self, collection: str | None = None) -> list[Document]: ...
    def delete(self, doc_id: str) -> bool: ...
    def stats(self) -> dict[str, Any]: ...
    def close(self) -> None: ...
```

```python
# storage/filters.py
Scalar = str | int | float | bool

@dataclass(frozen=True)
class Condition:
    field: str
    op: Literal["eq", "ne", "in", "nin", "gt", "gte", "lt", "lte"]
    value: Scalar | list[Scalar]

@dataclass(frozen=True)
class Filter:
    conditions: list[Condition] = field(default_factory=list)  # AND
    any_of: list["Filter"] = field(default_factory=list)       # OR 组
```

```python
# vectorstore/base.py
@dataclass
class VectorHit:
    id: str
    document: str
    metadata: dict[str, Any]
    distance: float
    score: float        # 0–1，越大越相关；由适配器按 metric 归一化
    metric: str         # cosine | l2 | ip
```

```python
# storage/factory.py
def make_vector_store(settings) -> VectorStore: ...
def make_document_store(settings) -> DocumentStore: ...
def make_study_store(settings) -> StudyStore: ...
```

配置：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `KB_VECTOR_STORE` | `chroma` | `chroma | qdrant | lancedb` |
| `KB_DOC_STORE` | `sqlite` | `sqlite | postgres` |
| `KB_STUDY_STORE` | `sqlite` | `sqlite | postgres` |
| `KB_VECTOR_METRIC` | `l2` | `cosine | l2 | ip`，需与模型一致 |

## 10. 一致性要求

1. **单一事实来源是源文件与 `documents.sqlite3`**，向量索引是派生数据，可重建
2. **跨存储一致性走 outbox**，不依赖数据库事务
3. **chunk 元数据必须携带 manifest 信息**，否则无法安全迁移
4. **契约测试是迁移的门槛**：新适配器通过同一套测试才算可替换
5. **`attempts` / `learner_state` 不可重建**，迁移时单独备份与校验

---

## 11. 参考

- [PLAN v4](../../PLAN.md) 第 8.3 节
- [知识治理](../knowledge-governance.md) 第 3、6 节
- [L1→L3 拆解](../mvp-l1-to-l3.md) 第 4 节
