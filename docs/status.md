# 项目进度

> 更新时间：2026-11-26
> 当前状态：**v0.1.0（L1 已交付）+ P0 完成 + 原件不双写已落地 + P1 可信与治理已落地 + P2 严格模式 / 过期冲突 / 近似去重已落地 + 学习切片（硬编码 3 题的 today → answer → 下次到期）已落地 + 本机 MCP 与打开原文已落地 + 同步通道预留已落地 + L2 知识结构（概念 / 出处 / 消歧 / 概念页 / 参照系覆盖率与缺口）已落地**
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
- **旧文档回填**：`kb governance backfill`。P1 起同时把 scope/status 写回已有切块

### P1 可信与治理（2026-11-10）

- **闸 1 可答性**：重排之后、生成之前。`top1 < τ_low` 直接拒答且不调用生成；中间带仍作答，但置信度降为 low，并提示「依据较弱」；达到 `τ_high` 正常作答。拒答是正常答案，写入 `answer_log.refusal_reason` / `confidence`
- **τ 要重标**：向量默认 `KB_ANSWER_TAU_LOW` / `KB_ANSWER_TAU_HIGH` = **0.52 / 0.54**。这是 2026-11-10 在 `examples/sample_notes` 上、用 `BAAI/bge-small-zh-v1.5`、L2 经 `1/(1+d)` 量出来的：库外 32 题 top1 最高 0.4959，基线五题最低 0.5440。重排默认 `KB_ANSWER_RERANK_TAU_LOW` / `KB_ANSWER_RERANK_TAU_HIGH` = **0.50 / 0.90**，尺子是 `BAAI/bge-reranker-base` 的 `predict`（sentence-transformers 里已经是 Sigmoid 概率；库外最高 0.3319，基线最低 0.9406）。概率不再做第二次 sigmoid。重排器同时认 `activation_fn`（4.x）和 `default_activation_function`（2.7–3.x）；两个都没有、且分数已经在 [0, 1] 时也不再 sigmoid。确认是 logit 时只做一次数值稳定的 sigmoid。换嵌入、度量、重排模型或语料后这些数都会失效，要用库外集和基线集重新看 top1 再改
- **闸 2 引用契约**：提示词只允许编号来源、事实句带 `[n]`、资料不足用固定句、末尾一行 `SELF_CHECK`。后检剥掉不在检索集合里的编号；全部编造或没有任何有效编号时改成固定拒答。不做闸 3 的陈述校验
- **库外拒答集**：`eval/datasets/out_of_kb.jsonl`（32 题，当前样例笔记覆盖不到）。`kb eval --mode refusal --dataset eval/datasets/out_of_kb.jsonl` 计算 `refusal_recall`。这个模式不调用裁判；没有 LLM key 时，低于 τ 的拒答仍然计入，检索分够高但生成失败的那条不算拒答
- **检索 fail-closed**：默认查询的向量 `where` 是 `status=active` 且 `scope $in` 允许列表（`default`、`work`，再加配置里的 default scope）。不用 `$nin` 排除 private/secret：Chroma 的 `$nin` 会把缺字段也算进去。缺 scope 或空 scope 不会返回。`retriever.search()` 不传这道过滤时由检索器补上；补不上就抛错，不会静默搜全库。`include_hidden=True` 才是旁路。文档行 / 治理元数据仍是第二道，隐藏近邻占满窗口时仍会扩大窗口
- **hide / unhide** 会把切块 `status` 写回去。`kb governance backfill` 给旧切块补 scope/status，否则缺这两项的旧切块不会被默认检索命中
- **级联**：`vector_index` 消费 `doc_events`（hide / unhide / delete / pending_cleanup / supersede / archive），失败进 `consumer_dead_letter` 后跳过。P1 当时 `mentions` / `items` / `reports` / `concept_pages` / `queue` / `cache` 只有名字和游标。L2 起 `mentions` / `concept_pages` 在 `study.enabled` 且 `study.concepts` 时才挂上；关掉时游标不动，也不创建学习库。`items` / `reports` / `queue` / `cache` 仍然只有名字

### P2 严格模式、过期冲突、近似去重（2026-11-11）

- **闸 3**：只在 `KB_ANSWER_STRICT=true` 时启用，默认关。答案拆成原子陈述，只按所引片段判断能否推出。推不出的陈述删掉并写入 gaps；失败率高于 `KB_ANSWER_STRICT_MAX_FAIL_RATE`（默认 0.5，严格大于）或一条都留不下来时，整题改成和闸 1 相同的固定拒答句。结果写入 `answer_log.verification_json`。没有 LLM key、也没有注入校验器时跳过，不改闸 1 / 闸 2，也不抛错
- **时效**：`docs_meta` 补上 `last_verified`。切块写入整数 `valid_until`（YYYYMMDD，未设置是 `99991231`），因为 Chroma 的 `$gte` 不接受日期字符串。默认检索在原有 `status=active` 且 `scope $in` 之外，再要求 `valid_until >= 今天`。文档行是第二道。`valid_until` 当天仍有效，次日起排除。显式 `--include-expired` 或 `as_of=YYYY-MM-DD` 可以拿出过期来源，并标注时间。完整的历史 as-of 查询仍是 P3
- **关系**：`doc_relations`（`supersedes` / `merged_into` / `contradicts` / `related`，`pending` / `confirmed` / `rejected`）。入库可以用显式 metadata、邻居余弦或 LLM 提议，全部先落成 pending，确认前不改变检索。`kb governance relations confirm` 之后，`supersedes` 把旧文档标成 `superseded` 并沿链指向最新；`contradicts` 两边都返回，并写明「存在两种说法」。不静默合并冲突
- **近似去重**：精确哈希跳过保持不变。SimHash 64 位、汉明距离 `<= 3` 才不写第二份文档索引，并记 `duplicate_of`。余弦 `>= 0.95` 只有在汉明距离也 `<= 3` 时才算同一份副本，避免把未声明的冲突折叠掉。切块同样不写第二份向量；如果一篇里只丢掉一部分切块，入库报告记在 `partial`，不算完整成功。检索先折叠这种副本，再 MMR（λ 默认 0.7），然后才是原来的重排。声明了关系或 `allow_near_duplicate` 的文档不会被当成冗余副本。自动合并只发生在精确哈希
- **阈值未重标**：余弦 0.95、汉明距离 3、MMR λ 0.7、邻居余弦 0.80 是文档化默认值，按 L2 归一化向量理解（默认嵌入 `BAAI/bge-small-zh-v1.5`）。余弦很高但 3-gram 距离大于 3 时不跳过、不折叠，避免吞掉未声明的冲突或复述。换嵌入、度量或语料后必须重标
- **评测集**：`eval/datasets/near_miss.jsonl`（12）、`conflict.jsonl`（6）、`expiry.jsonl`（6）。`kb eval --mode near_miss|conflict|expiry` 会临时入库数据集里的笔记，跑完删除。没有 LLM key 时部分覆盖识别率记为跳过（-1）；冲突呈现和过期排除不依赖模型。原有 `refusal` 模式还在
- **没有做**：题库去重、`items` / `reports`、一等历史查询、embedding 迁移演练。`mentions` / `concept_pages` 不在 P2 里做，已由 L2 接上。学习切片不补这些，只做硬编码复习闭环

### 学习切片（2026-11-12）

- **闭环**：`kb study today` 列出今天到期的 3 道硬编码题；`kb study answer <id> --response "..."` 做精确判分，并把下次到期写入 `study.sqlite3` 的 `reviews`。题目来自 `examples/sample_notes/01_rag基础.md`，不是模型生成
- **调度**：SM-2 简化版。第一次答对间隔 1 天，第二次 3 天，之后 `round(间隔 × ease)`；答错间隔回到 1 天，ease 减 0.2，重复次数清零。到期日之前再答（含同一天再答）仍写入 `attempts`，但不改 `next_due`、间隔、ease、reps、lapses，返回 `schedule_updated=false`
- **日历**：学习队列的「今天」固定 Asia/Shanghai，不看进程本地时区。P2 的 `valid_until` 仍用 UTC 日，这刀没有改那只钟。上海 0:00–8:00 时，UTC 日还是前一天
- **开关**：只看 `study.enabled`。库与组件默认关。`kb study enable` 不回填历史，第一次 `today` / `answer` 才种下这 3 题。关闭不删库，核心摄入 / 检索 / 问答不打开这张库
- **没有做**：这刀不抽取概念，不做 learner_state、周报、S1 正式出题、题库去重、托盘 / 小窗。`study.queue` 等子开关仍只是注册项，这刀不读它们。连续使用 3 天的 Go/No-Go 还没做。概念、出处、消歧、概念页和参照系是后面的 L2，不改这刀的日历，也不改未到期只记账

### 本机 MCP 与打开原文（2026-11-12）

- **MCP**：`kb mcp` 在本机 stdio 上提供知识库组件，不另开端口。工具是 `search`、`ask`、`open_original`。可见范围与默认检索相同：隐藏、private、secret、已过期和不在允许 scope 里的文档不返回，调用方也不能用 `include_hidden` 或自定义 `where` 旁路。`study.enabled` 打开后才列出 `study`（`today` / `answer`），并发送 `notifications/tools/list_changed`，客户端不用重连。关闭时调用 `study` 失败，并且不创建 `study.sqlite3`
- **打开原文**：`kb open <doc_id>` 与 MCP `open_original` 只用该文档已经保存的绑定路径，并且和默认检索同一道门（`_default_retrievable`）。隐藏、private、secret、已过期，以及不在默认 scope 允许列表（`default` / `work`，再加配置的 default scope）里的文档都不能打开。没有 `--include-expired` 旁路。优先 Obsidian，启动失败再改用系统默认应用。不接受路径，不列目录，不读正文，不执行 shell。失败和成功都不带绝对路径
- **路径**：MCP 与 REST 共用 `public_source_path`。`KB_EXPOSE_SOURCE_PATHS` 默认关，绝对 `source_path` / `image_path` 置空；相对路径仍返回。打开原文由本机服务完成，调用方不需要绝对路径
- **ADR 0002**：打开原文以及 MCP 的检索 / 问答都不把绑定原件的整篇正文写回 `documents.sqlite3`
- **没有做**：托盘 / 小窗、S1、题库去重。MCP 不暴露概念命令。L2 的概念、概念页和参照系见后面一节，不从这刀的 stdio 工具面进去。同步通道预留见下一节

### 同步通道预留（2026-11-25）

- **许可**：默认全部关闭，不上云。`remote`（加密远程）和 `lan`（同 Wi-Fi 配对）分开，互不包含。只打开 `remote` 时，元数据、摘要、切块、嵌入、学习进度、时间线可以进入以后的传输；健康数据还要 `health_remote`。只打开 `health_remote` 不够。收回 `remote` 之后，远程健康不再有效，单独的健康许可还留着，再次打开 `remote` 时恢复。局域网许可打开后，健康数据随这条通道走，因为单独许可只约束远程。原件全文两条通道都拒绝
- **稳定 id**：设备 `dev_`、文件夹 `vlt_`、记录 `rec_`，后面各加 32 位十六进制。不由本机路径、主机名或文档内容哈希派生。已有 `doc_id` 仍是本机文档主键，可以记在记录上当本机指针，不是跨设备主键
- **本机路径映射**：文件夹的稳定 id 对到这台设备上的一个目录。目录换地方用 `kb sync rebind`，稳定 id 不变。记录位置是文件夹内的相对路径；改相对路径不换稳定 id。绝对路径不是主键。命令输出不带绝对路径。符号链接如果指向映射以外，拒绝登记。中间目录已经是指向外部的符号链接时，末端文件还不存在也拒绝，失败信息不带绝对路径
- **库**：`sync.sqlite3`。核心摄入 / 检索 / 问答不创建它，也不读它。库里没有原件全文
- **没有做**：不传输、不加密打包、不配对、不做移动端、不写托盘 / 小窗。这刀不开始 L2 / S1。L2 已另行落地，同步预留本身没有改。见 ADR 0002

### L2 知识结构（2026-11-26）

- **概念与关系**：`study.enabled` 和 `study.concepts` 都打开时，入库之后异步消费 `doc_events`。消费者是 `mentions` 和 `concept_pages`。产出概念、白名单关系（`prerequisite` / `part_of` / `implemented_by` / `measured_by` / `related`）和 mentions。出处是切块里的连续片段，最长 180 字，不是整篇原文
- **消歧**：NFKC、去空白、去标点、小写之后，规范化名和别名表精确命中才自动合并。嵌入相似度 `>` 0.88 且类型相同只记建议，`kb study concepts merge` 才合并。0.88 是 mvp 文档里的默认值，这次没有按新语料重标。重复率是消歧之后仍共享规范化名或别名的多余概念占比，`kb study concepts --audit` 会打印，目标 `< 10%`
- **有意义**：自动规则是名称 2–40 字、不在泛词表、定义至少 8 字，并且名称或定义能在出处片段里对上。`--audit` 打印的是这条规则覆盖的比例，目标 `≥ 90%`。它不代替 [mvp 拆解](./mvp-l1-to-l3.md) 里「20 篇真实笔记人工抽检、概念能否单独出题」
- **概念页**：`kb study concept <id>` 聚合定义、出处、相关笔记。没有 `learner_state`，掌握度写「尚无学习记录」。派生 Markdown 在存储目录的 `concept_pages/`，不写回源目录
- **参照系**：`kb study outline import` 与 `kb study outline coverage`。覆盖率是有可见 mention 的节点比例。缺口是没有可见 mention 的节点。掌握度覆盖率在没有学习记录时留空，不写成 0
- **开关**：只开 `study.enabled`、关掉 `study.concepts` 时不抽取。打开把 `mentions` / `concept_pages` 的游标放到当前事件末尾，不回放历史，也不自动回填。`kb study backfill` 才重放，`--dry-run` 只统计。关闭不删已有派生数据。核心摄入 / 检索 / 问答不依赖这次抽取，抽取失败只进 dead letter
- **LLM**：`KB_STUDY_LOCAL_MODEL` 优先走本地端点（默认 `http://127.0.0.1:11434/v1`）。没有本地模型时，只有设置了 `KB_STUDY_EXTRACT_API_KEY` 才调用云端；提供商默认 DeepSeek，也可用 `KB_STUDY_EXTRACT_PROVIDER=openai`。没有这把 key 时用规则抽取，不访问网络，也不借用问答的 `KB_LLM_API_KEY`。`KB_STUDY_EXTRACT=heuristic` 或 `off` 可以关掉云端。仓库里不放真实 key
- **可见性**：隐藏、private、secret、已过期、不在默认 scope 允许列表，以及 `exclude_from_learning` 的文档，不出现在默认概念列表、概念页和参照系覆盖率里。隐藏不删已写上的 mention。命令输出不带绝对路径
- **存储**：概念、关系、mentions、参照系在 `study.sqlite3`。不把原件全文写入 `documents.sqlite3` 或 `study.sqlite3`。源文件哈希保持不变，不自动改写或合并源笔记。向量库和 study 库不包在同一个事务里。同一 `chunk_id` + 内容哈希 + `KB_STUDY_PROMPT_VERSION`（默认 `r1`）只抽一次
- **每篇上限**：每个切块最多 3 个概念，每篇新概念最多 7 个。已经有的概念仍可挂出处
- **没有做**：S1 正式出题、`learner_state`、周报、托盘 / 小窗、人生时间线、移动端、完整同步传输。学习切片的 Asia/Shanghai「今天」和「未到期再答只记账」没有改。P1 的 τ、P2 的 UTC 时效钟没有改

### 原件不双写（2026-11-04，ADR 0002）

- **绑定 Vault / 文件夹**：SQLite 只存源路径、内容哈希、元数据、摘要、切块定位，不存整篇正文
- **向量库**：仍保存切块文本与嵌入（检索用派生副本）
- **父文档扩写**：按路径回读并重新清洗源文件，再按切块定位截取。先核对 `content_hash`，不一致则退回切块文本。`KB_MAX_PARENT_CHARS` 只限制返回窗口
- **inline**：没有外部原件的提交（含 API 文本和上传）正文留在应用库，来源标为 `inline`
- **已入库父全文**：`kb governance migrate-parents`，或 admin `POST /v1/governance/migrate-parents`。仅当源文件哈希与已入库哈希一致时删除库内全文并标为 `bound`；不一致则保留正文并标为 `diverged`（提示重新入库）。源文件不在则标为 `orphaned_copy`。定位从已有切块对齐，不按当前切块器重切

## 2. 当前验证

- 全量测试：**290 passed**（2026-11-26，含 L2 概念 / 出处 / 消歧 / 概念页 / 参照系，以及更新文档后刷新派生数据、抽取密钥不借用问答密钥、按已存储别名消歧）。这次没有重跑 `refusal_recall`，不把以前的通过数当成这次的结果
- `STRUCTURE.md` 已补上源文件回读与父全文迁移相关文件
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

- 默认检索的向量 `where` 要求 `status=active`，`scope` 落在允许列表（`default` / `work`，或配置的 default scope）里，并且整数 `valid_until >= 今天`（未设置是 `99991231`）。缺 scope、空 scope、private、secret、已过期都不会被这道 `where` 返回。文档行和治理元数据仍是第二道。升级前缺这些元数据的旧切块要跑一次 `kb governance backfill`
- 同路径再入库或替换时，如果调用方没有显式传入新的 scope，会保留原来的 scope 和 hidden / private 状态，不会洗成默认可检索。显式传入 scope 才会按新范围写入并把状态放回 active
- 多个进程共用一个 Chroma 目录时，另一个进程的 `kb repair` 或向量改写会让本进程已经加载的内存索引过期：`get` 能看到新记录，`query` 仍可能返回已删除的 id，默认检索因此变空。下一次检索会对比目录签名并重新打开客户端。跨进程改写之后，同一进程里随后的检索可能因为 mtime/fingerprint 继续漂移而每次都重新打开；结果仍然正确，本轮不处理。`kb repair` 的结果里有 `notice`，命令行也会打到 stderr：若检索仍然为空，重启那个进程。Chroma 不保证多个进程同时写入同一目录是安全的
- `answer_log` / `audit_log` 暂无保留期与轮转策略
- 学习切片仍是 3 道硬编码题的 today → answer → 下次到期。正式题库、每日队列策略、周报、托盘都还没有。L2 在同一张 `study.sqlite3` 里加了概念、关系、mentions 和参照系，没有 `learner_state`，也没有 S1 出题。按 PLAN §0.1，学习仍是独立模块：托盘默认开，库与组件默认关。`study.enabled=false` 时核心不创建、也不读取 `study.sqlite3`。只开总开关、关掉 `study.concepts` 时不抽取。打开概念开关不回填历史。没有抽取 key、也没有本地模型时用规则，不调用云端。相似度 0.88 只记建议合并，这次没有重标。学习「今天」是 Asia/Shanghai；P2 过期比较仍是 UTC。未到期的作答只记账，不推进下次到期
- 父文档扩写会整篇回读并重新清洗源文件；`KB_MAX_PARENT_CHARS` 只限制返回窗口
- 绑定文件在入库后被修改时，扩写核对 `content_hash`，不一致则退回切块文本，不把漂移后的文件送进提示词；文件缺失同样退回切块文本。切块与源文件重新对齐要重新入库
- 对外 REST / MCP 默认去掉绝对 `source_path` / `image_path`（`KB_EXPOSE_SOURCE_PATHS` 默认关）。相对路径仍返回。`GET /v1/documents/{doc_id}` 默认不含全文，`include_text=true` 才返回 inline 正文。打开原文用 `kb open <doc_id>` 或 MCP `open_original`，门和默认检索相同：过期或不在允许 scope 里的文档打不开，也没有 `--include-expired` 旁路。本机按已绑定路径调用 Obsidian，失败再改用系统默认应用。调用方拿不到绝对路径
- `content_hash` 与新文档 `doc_id` 存完整 SHA256（64 hex）。展示可以只用前 12 hex（`hash_display`），去重和主键不用截断值。见下方「哈希迁移」
- `orphaned_copy` 表示源文件已经不在，库内全文留到用户处理。把文件放回且哈希一致后再跑 `migrate-parents`，会升为 `bound` 并删掉副本
- `diverged` 表示源文件还在，但和已入库哈希不一致（不用 `orphaned_copy`，因为文件没丢）。迁移保留正文，并在结果里提示重新入库。文件改回与哈希一致后再跑一次，才会升为 `bound` 并删掉副本
- 闸 3 声明校验、过期/冲突和近似去重已在 P2 落地。题库去重、items / reports、一等历史查询、embedding 迁移演练还没有。mentions / concept_pages 在 L2：概念开关关闭时不消费。升级前缺 `valid_until` 的旧切块要再跑一次 `kb governance backfill`，否则默认检索的时效 `where` 不会命中它们
- 同步通道预留已落地：许可模型、稳定 id、本机路径映射，默认关闭，不传输内容，不做移动端。托盘 / 小窗尚未实现。本机 MCP（stdio）和按绑定路径打开原文已落地
- 工作知识管理不是当前 MVP

## 4.1 哈希迁移（12 hex → 64 hex）

2026-11-05 之前，`content_hash` 和 `doc_id` 只存 SHA256 的前 12 hex。48 bit 碰撞时 `get_by_hash` 会取到一行并跳过另一篇入库。

升级后的行为：

- 新写入的 `content_hash`、`doc_id` 都是 64 hex。数据库不用改表，列本来就是文本。
- 已有行的 12 hex 主键**保持不变**，向量和引用不用重写。再次入库同一路径，或同一标题下全文仍能对上的 inline 文档，会沿用这条 `doc_id`，并把 `content_hash` 写成完整 SHA256。路径或全文对不上时不覆盖，新文档用完整 id。
- 去重只认完整哈希。多行共用一个哈希且全文不一致时不任取一行，也不会因此跳过新内容。
- 父文档扩写仍接受旧的 12 hex：它必须是当前文件完整哈希的前缀，否则当作漂移，退回切块文本。
- `kb governance migrate-parents` 会在源文件或库内正文对得上时，把 12 hex 升成 64 hex（结果里计为 `bound` 或 `hash_upgraded`）。升完后重复执行是 `unchanged`。
- 评测集 `eval/datasets/baseline.jsonl` 已改成完整 `doc_id`。2026-11-04 QA 记录里的 12 hex 是这些 id 的前缀，不是当前主键。

## 5. 下一步

近期顺序以本节为准。治理阶段的 P1、P2 与 [知识治理](./knowledge-governance.md) 同名。学习阶段叫 L2、S1–S4，见 [PLAN §6](../PLAN.md)。

1. **P2 已落地**（2026-11-11）：闸 3 / `KB_ANSWER_STRICT`、过期与冲突、近似去重、近邻陷阱集 / 冲突集 / 过期集。题库去重仍等学习模块。P1 的闸门、引用契约、库外拒答集和 fail-closed scope 没有在这里重做。items / reports 仍等后续学习阶段。mentions / concept_pages 在第 5 条 L2
2. **学习切片已落地**（2026-11-12）：3 道硬编码题，`kb study today` → `kb study answer` → 下次到期。今天按 Asia/Shanghai。未到期再答只记账。这刀不抽取概念，不是 S1。托盘或小窗没有做。正式闪卡生成仍是 S1，排在第 5 条 L2 之后。L2 已落地，见第 5 条。连续使用 3 天的 Go/No-Go 还没做
3. **本机 MCP 与打开原文已落地**（2026-11-12）：`kb mcp`（stdio）提供 search / ask；`study.enabled` 打开后才有 study，并通知工具列表变化。`kb open <doc_id>` 与 MCP `open_original` 用已绑定路径打开原文，门和默认检索相同（含过期与 scope 允许列表），没有 `--include-expired` 旁路。优先 Obsidian，启动失败再用系统默认应用。对外响应默认去掉绝对路径。不监听端口，也不是通用文件或 shell 工具。托盘 / 小窗没有做。MCP 不提供概念工具。L2 见第 5 条。同步通道预留见下一条
4. **同步通道预留已落地**（2026-11-25）：许可模型、稳定 id、本机路径映射。`remote` 与 `lan` 分开，健康数据进入远程要单独许可，原件全文不进这条通道。不做移动端，也不做完整同步传输
5. **L2 知识结构已落地**（2026-11-26）：概念、出处、消歧、概念页，以及参照系的覆盖率与缺口。掌握度在没有学习记录时留空。做完才进入 S1 正式出题；S1 还没做
6. **人生时间线前置**：健康和敏感范围只走本地模型或直接拒绝；这些问答不进明文日志
7. **人生时间线**：事实、事件、健康。工作模块不是当前 MVP

## 6. 恢复方式

```bash
# 运行回归
.venv/bin/python -m pytest -q

# 查看运行时开关
.venv/bin/kb config get

# 本机 MCP（stdio，不监听端口）。学习模块关闭时没有 study 工具
.venv/bin/kb mcp

# 用已绑定路径打开原文。优先 Obsidian，否则系统默认应用。不接受路径
.venv/bin/kb open <doc_id>

# 同步通道预留。默认不上传。不传输内容，也不打印绝对路径
.venv/bin/kb sync status
.venv/bin/kb sync grant remote
.venv/bin/kb sync grant health-remote
.venv/bin/kb sync bind <folder>
.venv/bin/kb sync id <vault_id> <relative>
.venv/bin/kb sync revoke remote

# 学习模块。默认关闭。开启不回填历史。
# study.concepts 默认开：只处理打开之后的新入库。旧文档用 backfill。
# 没有抽取 key 时走规则，不调用云端。关闭不删已有派生数据。
.venv/bin/kb study enable
.venv/bin/kb study today
.venv/bin/kb study answer rag-fullname --response "Retrieval-Augmented Generation"
.venv/bin/kb study concepts --audit
.venv/bin/kb study concept <concept_id>
.venv/bin/kb study concepts merge <keep> <drop>
.venv/bin/kb study outline import <file.yaml>
.venv/bin/kb study outline coverage --domain rag
.venv/bin/kb study drain
.venv/bin/kb study backfill --dry-run
.venv/bin/kb study disable

# 查看治理状态
.venv/bin/kb embedding
.venv/bin/kb events
.venv/bin/kb answer-log
.venv/bin/kb audit-log

# 旧库补充 docs_meta，并给旧切块补上 scope/status/valid_until
# 缺 valid_until 的旧切块不会被默认检索的时效 where 命中
.venv/bin/kb governance backfill

# 消化 doc_events。概念开关打开时连同 mentions / concept_pages；否则它们仍在 residual
.venv/bin/kb governance drain

# 库外拒答：refusal_recall。不调用裁判
.venv/bin/kb eval --mode refusal --dataset eval/datasets/out_of_kb.jsonl

# 近邻陷阱 / 冲突 / 过期。数据集自带笔记，跑完会删掉临时文档
# 没有 LLM key 时，部分覆盖识别率记为跳过；冲突呈现和过期排除仍然计算
.venv/bin/kb eval --mode near_miss --dataset eval/datasets/near_miss.jsonl
.venv/bin/kb eval --mode conflict --dataset eval/datasets/conflict.jsonl
.venv/bin/kb eval --mode expiry --dataset eval/datasets/expiry.jsonl

# 关系先是 pending。确认后才改变检索；拒绝则保持或收回
.venv/bin/kb governance relations list --status pending
.venv/bin/kb governance relations confirm <src_id> <dst_id> supersedes
.venv/bin/kb governance relations reject <src_id> <dst_id> contradicts

# 迁出已入库父全文（源文件缺失的记录标为 orphaned_copy）
# REST（admin Key）：POST /v1/governance/migrate-parents
.venv/bin/kb governance migrate-parents
```

架构与数据流见 [architecture.md](./architecture.md)；阶段与验收见 [PLAN v4](../PLAN.md) 与 [L1→L3 拆解](./mvp-l1-to-l3.md)。
