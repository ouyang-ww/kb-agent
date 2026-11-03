# 项目进度

> 更新时间：2026-10-30
> 当前状态：**v0.1.0（L1 已交付）+ P0 完成**
> 配套：[PLAN v4](../PLAN.md) · [架构设计](./architecture.md) · [知识治理](./knowledge-governance.md)

---

## 1. 已完成

### L1 检索助手（v0.1.0）

- 摄入：loaders → cleaner → chunker → 索引；哈希去重、更新/删除
- 检索：意图识别 → 查询改写 → 向量检索 → 重排 → 父文档扩写
- 生成：编号引用、多模态图片描述、降级策略
- 接口：CLI / Gradio / FastAPI / Python API
- 评估：Recall@k / Accuracy / Faithfulness / 对比实验
- 测试：原有 71 个基线测试全部保持

### P0（2026-10-30 完成）

- **存储抽象**：`VectorStore` / `DocumentStore` Protocol、中立 Filter、工厂、契约测试
- **运行时开关**：`runtime_flags` + `FeatureFlags`，支持热切换与跨进程刷新
- **治理骨架**：`docs_meta` / `doc_events` / `consumer_cursor`
- **scope 隔离**：`default` / `private` / `secret`；隐藏文档默认不出现在检索、列表、问答
- **隐藏/恢复**：`kb hide` / `kb unhide`，REST 对应端点
- **答案日志**：`answer_log`，记录 query / 检索结果 / 答案 / 引用 / 延迟
- **审计日志**：`audit_log`，记录 hide / unhide / delete / flag 变更 / backfill
- **向量版本**：`embedding_manifest` + `runtime_pointer`（`active_embedding`）
- **API 鉴权**：只读 Key / admin Key 分权，常量时间比较；旧 `KB_API_KEY` 兼容为 admin
- **旧文档回填**：`kb governance backfill`

## 2. 当前验证

- 全量测试：**110 passed in 12.69s**（2026-10-30）
- `STRUCTURE.md` 目录树：98/98 一致
- 测试历史：本地 `test_history.log`（已 gitignore，不随仓库发布）

## 3. 主要提交

```
3a27c59 feat: complete remaining P0 observability and access controls
7f65f61 docs: mark P0 governance and runtime flags as implemented
7467cca feat: add governance metadata, events, and scope filtering
11bbead feat: refresh runtime flags across processes
003630c feat: add runtime feature flags and hot toggling
5b26e8d docs: add system architecture diagrams and data flows
```

## 4. 已知边界

- scope 过滤目前是 **doc-level 后过滤 + chunk 元数据**；存储层强制过滤与旧向量元数据回填待后续强化
- `answer_log` / `audit_log` 暂无保留期与轮转策略
- 学习扩展（概念、题库、队列、周报）尚未实现。按 PLAN §0.1，它是当前 MVP 主路径，仍然是独立模块：托盘默认开，库与组件默认关
- v0.1.0 仍把父全文写入 SQLite；目标是迁出，见 ADR 0002
- 过期/冲突检测、严格模式、去重尚未实现
- 同步通道、托盘 / 小窗、MCP、打开原文尚未实现。MVP 不做移动端
- 工作知识管理不是当前 MVP

## 5. 下一步

近期顺序以本节为准。治理阶段的 P1、P2 与 [知识治理](./knowledge-governance.md) 同名。学习阶段叫 L2、S1–S4，见 [PLAN §6](../PLAN.md)。

1. **原件不双写**：按 ADR 0002 把已入库父全文迁出，扩写改为回读源文件
2. **P1 可信与治理**：可答性闸门、引用契约、级联 consumers、库外拒答评测集、检索 fail-closed scope
3. **P2 严格模式**：声明校验、过期/冲突检测、近似去重
4. **学习切片**：硬编码几道题、作答、下次到期。不抽取概念，不等 L2。托盘或小窗可以在此起步。正式闪卡生成是 S1，排在第 7 条 L2 之后
5. **本机 MCP**，以及用 Obsidian 或系统默认应用打开原文
6. **同步通道预留**：许可模型、稳定 id、本机路径映射。不做移动端，也不做完整同步传输
7. **L2 知识结构**：概念、出处、消歧、概念页。做完再进入 S1 正式出题
8. **人生时间线前置**：健康和敏感范围只走本地模型或直接拒绝；这些问答不进明文日志
9. **人生时间线**：事实、事件、健康。工作模块不是当前 MVP

## 6. 恢复方式

```bash
# 运行回归
.venv/bin/python -m pytest -q

# 查看运行时开关
.venv/bin/kb config get

# 查看治理状态
.venv/bin/kb embedding
.venv/bin/kb events
.venv/bin/kb answer-log
.venv/bin/kb audit-log

# 旧库补充 docs_meta
.venv/bin/kb governance backfill
```

架构与数据流见 [architecture.md](./architecture.md)；阶段与验收见 [PLAN v4](../PLAN.md) 与 [L1→L3 拆解](./mvp-l1-to-l3.md)。
