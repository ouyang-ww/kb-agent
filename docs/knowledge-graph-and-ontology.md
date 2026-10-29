# 知识图谱与本体：学习笔记

> 定位：概念梳理 + 与 RAG 的结合方式 + 在 kb-agent 中可能的落地路径。
> 状态：**纯学习文档**，与 [PLAN v4](../PLAN.md) 的 L2 知识结构相关，不引入任何代码依赖。
> 适用读者：想判断"我的知识库要不要上图"的开发者。

---

## 目录

1. [三个容易混淆的概念](#1-三个容易混淆的概念)
2. [本体由什么组成](#2-本体由什么组成)
3. [本体相关的标准与技术栈](#3-本体相关的标准与技术栈)
4. [知识图谱的两种数据模型](#4-知识图谱的两种数据模型)
5. [构建流水线](#5-构建流水线)
6. [GraphRAG：图谱如何接入 RAG](#6-graphrag图谱如何接入-rag)
7. [和 kb-agent 的关系](#7-和-kb-agent-的关系)
8. [成本、收益与常见坑](#8-成本收益与常见坑)
9. [学习资源](#9-学习资源)
10. [术语速查](#10-术语速查)

---

## 1. 三个容易混淆的概念

日常讨论里"知识图谱"常被用来指三样不同的东西，先分清：

| 概念 | 是什么 | 类比 |
| --- | --- | --- |
| **词表（vocabulary）** | 受控的术语集合，如 SKOS 的"概念 + 上下位 + 同义" | 字段的枚举值 |
| **本体（ontology）** | **schema**：类、属性、关系，以及约束与公理 | 数据库的 DDL，但支持推理 |
| **知识图谱（knowledge graph）** | **数据**：按本体（显式或隐含）实例化出的实体与关系 | 数据库里的行 |

一句话：**本体是模型，图谱是实例数据**。

- 可以只有本体没有图谱（一个空 schema）；
- 也可以只有图谱没有显式本体（schema 隐含在数据里，Neo4j 项目大多如此）；
- 二者在逻辑上对应 **TBox**（术语层：类与关系）和 **ABox**（断言层：个体的断言）。

### 1.1 开放世界 vs 封闭世界

这是本体建模最反直觉、也最常被误解的一点：

- **OWL 默认开放世界假设（OWA）**："没写 ≠ 不存在"。因此推理机不会因为"某人没写父母"就报错。
- **数据库是封闭世界假设（CWA）**："没写 = 不存在/不满足"。
- **唯一命名假设（UNA）**：OWL 里 `:A` 和 `:B` 可能是同一个东西（除非声明 `owl:differentFrom`）；数据库里两行就是两个东西。

实践含义：**别指望 OWL 帮你做数据校验**。要"必填、基数、值域检查"用 **SHACL**；要"推出新知识"（如传递闭包、子类归并）用 OWL。

---

## 2. 本体由什么组成

一个最小的"个人知识库"本体，用 RDF Turtle 写出来：

```turtle
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix ex:   <http://example.org/kb#> .

# ---- 类 ----
ex:Document a owl:Class .
ex:Concept  a owl:Class .
ex:Person   a owl:Class .

# 子类：技术概念是概念的子类
ex:Technology a owl:Class ; rdfs:subClassOf ex:Concept .

# ---- 关系（对象属性）----
ex:mentions   a owl:ObjectProperty ;
              rdfs:domain ex:Document ; rdfs:range ex:Concept .

ex:authoredBy a owl:ObjectProperty ;
              rdfs:domain ex:Document ; rdfs:range ex:Person .

# 公理：prerequisite 具有传递性（A 前置 B、B 前置 C ⇒ A 前置 C）
ex:prerequisite a owl:ObjectProperty , owl:TransitiveProperty ;
                rdfs:domain ex:Concept ; rdfs:range ex:Concept .

# 公理：Document 与 Person 不相交
ex:Document owl:disjointWith ex:Person .
```

对应的实例数据（ABox）：

```turtle
ex:note-001 a ex:Document ;
    rdfs:label "RAG 基础" ;
    ex:mentions ex:vector-search ;
    ex:authoredBy ex:ouyang .

ex:vector-search a ex:Technology ;
    rdfs:label "向量检索" ;
    ex:prerequisite ex:embedding .
```

把五类要素拆开看：

| 要素 | 说明 | 例子 |
| --- | --- | --- |
| **类 (Class)** | 概念的集合 | `Document`、`Concept`、`Person` |
| **个体 (Individual)** | 类的实例 | `ex:vector-search` |
| **属性 / 关系 (Property)** | 二元关系；分**对象属性**（个体↔个体）与**数据属性**（个体↔字面量） | `mentions`、`authoredBy`、`publishedYear` |
| **公理 (Axiom)** | 子类、传递性、函数性、不相交等 | `owl:TransitiveProperty` |
| **约束 (Constraint)** | 基数、必填、值域，用 SHACL 表达 | "每个 Document 至少一个作者" |

本体的价值在于三件事：**共享的语义契约**（不同来源能对齐）、**可推理**（显式写一部分，推出另一部分）、**可实现校验**（SHACL）。

---

## 3. 本体相关的标准与技术栈

| 标准 | 作用 | 一句话 |
| --- | --- | --- |
| **RDF** | 数据模型 | 一切皆三元组 `(主语, 谓词, 宾语)`，用 IRI 全局标识 |
| **RDFS** | 最轻的 schema | 类、子类、domain/range，附带极弱的推理 |
| **OWL 2** | 重量级本体语言 | 基于描述逻辑（DL），有推理语义；分 EL / QL / RL 三个 profile |
| **SHACL** | 约束与校验 | 声明"什么样的图是合法的"，不负责推理 |
| **SKOS** | 词表/分类法 | 同义、上下位、相关，适合做标签体系而非领域建模 |
| **SPARQL 1.1** | 查询语言 | 包括属性路径（`+`、`*`）、 federation、UPDATE |

**常用的复用本体**：`schema.org`（通用实体）、`FOAF`（人/社交）、`Dublin Core`（文献元数据）、`SKOS`（词表）、以及各领域标准（生物医学的 GO/SNOMED、出版业的 schema.org/Book 等）。

**工具**：

- 建模：**Protégé**（事实标准，免费开源）
- 推理机：HermiT、ELK（OWL EL，大图友好）、Pellet
- RDF 存储：Apache Jena（TDB2）、Virtuoso、Blazegraph、Oxigraph
- 校验：pySHACL、Apache Jena SHACL

**OWL 2 的 Profile 怎么选**：

- **OWL 2 EL**：实体多、关系简单的领域（生物医学典型），推理快
- **OWL 2 QL**：想用关系数据库查询改写（OBDA）
- **OWL 2 RL**：想用规则引擎实现（RDFox 等）

个人/小型项目通常**根本不需要 OWL**，RDFS 级别甚至一个 YAML 白名单就够了，见第 7 节。

---

## 4. 知识图谱的两种数据模型

| | **RDF 图** | **属性图（Property Graph）** |
| --- | --- | --- |
| 数据形态 | 三元组 (s, p, o)，字面量带数据类型 | 节点 + 边，两者都可挂键值属性 |
| 模式 | 用 RDFS/OWL 表达 | 可选（Neo4j 可无 schema，也可加约束） |
| 查询 | SPARQL | Cypher / GQL（ISO/IEC 39075:2024，2024 年成为国际标准） |
| 代表实现 | Jena、Virtuoso、Blazegraph | Neo4j、NebulaGraph、JanusGraph、Kùzu |
| 强项 | 标准化、可推理、多源语义融合 | 工程友好、路径查询直观、生态与工具链成熟 |
| 弱项 | 学习曲线陡、工具重、性能调优门槛高 | 语义标准弱、跨源对齐靠自建 |
| 边能否带属性 | 原生不支持（需具体化 / RDF-star 扩展） | 原生支持 |

同一件事在两种模型里的写法：

**属性图（Cypher）**

```cypher
// 写入
MERGE (d:Document {id: "note-001"})
MERGE (c:Concept  {name: "向量检索"})
MERGE (d)-[:MENTIONS]->(c);

// 查询：哪些文档提到了向量检索
MATCH (c:Concept {name: "向量检索"})<-[:MENTIONS]-(d:Document)
RETURN d.id;

// 多跳：RAG 的直接/间接前置概念
MATCH (c:Concept {name: "RAG"})-[:PREREQUISITE*1..2]->(p:Concept)
RETURN DISTINCT p.name;
```

**RDF 图（SPARQL）**

```sparql
# 查询：哪些文档提到了向量检索
SELECT ?doc WHERE {
  ?doc ex:mentions ex:vector-search .
}

# 多跳：属性路径 + 表示传递闭包（配合 owl:TransitiveProperty 也可由推理机得出）
SELECT DISTINCT ?p WHERE {
  ex:RAG ex:prerequisite+ ?p .
}
```

**选型建议（针对个人知识库/中小项目）**：

- 想快速跑通、团队只关心"查关系"→ **属性图**（Neo4j / Kùzu / 甚至 SQLite 两张表）
- 有多源异构数据要融合、需要形式化语义、或对接领域标准 → **RDF + OWL**
- 规模小、只是辅助检索 → **先用关系表实现**，不必上图数据库。图的本质是"建模方式"，不是"必须装 Neo4j"。

---

## 5. 构建流水线

```
① 本体设计（复用优先，自定义兜底）
        ↓
② 信息抽取：实体识别(NER) + 关系抽取(RE) → 三元组
        ↓
③ 实体消歧 / 对齐 / 共指：同一个实体别变成多个节点
        ↓
④ 入库（图存储 / 关系表）
        ↓
⑤ 查询、推理、与向量检索融合
```

### 5.1 ① 本体设计：先小后大

实践原则：

1. **复用优先**：能用 schema.org / FOAF 的类型就别新造
2. **核心先小**：先定 5–10 个实体类型、5–10 个关系类型，跑通再加
3. **标识稳定**：实体用 IRI 或稳定的业务 ID，不要用显示名当主键
4. **多语言标签分离**：`rdfs:label` 存标签，ID 与标签解耦
5. **版本化**：本体文件进 git，改动可追溯

对 RAG 场景，往往只需要一个 **"本体-lite"** YAML：

```yaml
# 只是白名单，不是形式化本体；够表达"允许出现什么"
entity_types: [Concept, Tool, Person, Project]

relation_types:
  mentions:     { domain: Document, range: [Concept, Tool, Person] }
  prerequisite: { domain: Concept,  range: Concept, transitive: true }
  authored_by:  { domain: Document, range: Person }
  part_of:      { domain: Concept,  range: Concept }
  uses:         { domain: Project,  range: Tool }
```

### 5.2 ② 信息抽取：LLM 是主力，但不是全部

传统 NER+RE 模型（spaCy、HanLP、BERT 关系分类）准确、可控，但需要标注数据。现在更常见的做法是 **LLM 零样本/少样本抽取 + 规则校验**。

要点：

- **严格限定输出 schema**（只允许白名单类型），否则模型会"发明"关系
- **要求证据片段**（evidence），便于过滤幻觉、便于溯源回原文
- **按 chunk 抽取后合并**，不要整篇一次抽（长文召回差、也更容易漏）
- **输出 JSON 而非自然语言**，便于程序校验

一个可用的 prompt 骨架：

```text
你是信息抽取器。只能使用以下类型，禁止发明新类型。
实体类型：Concept / Tool / Person / Project
关系类型：mentions / prerequisite / authored_by / part_of / uses

输入：<文档片段>
输出 JSON：
{
  "entities": [{"name": "...", "type": "Concept", "aliases": ["..."]}],
  "relations": [{"head": "...", "relation": "prerequisite", "tail": "...",
                 "evidence": "原文中支持该关系的连续片段"}]
}
若某条关系在原文中找不到证据，不要输出。
```

### 5.3 ③ 实体消歧：真正的工作量所在

这是图谱质量的决定性环节。常见的多步策略：

1. **规范化**：全半角、大小写、空白、标点统一
2. **别名表**：人工维护高频实体的别名（"RAG" / "检索增强生成" / "Retrieval-Augmented Generation"）
3. **嵌入聚类**：对实体名 + 描述做向量化，相似度超阈值候选合并
4. **类型与上下文约束**：同名的 `Python`（语言）和 `Python`（蛇）靠类型/上下文区分
5. **人工抽检**：小规模场景下，抽检 20–50 个实体的合并结果，往往比调阈值有效

**同名不同实体**和**异名同实体**是两个方向的问题，要分别处理。消歧做不好，图谱会变成一堆孤立碎片，比不做还差。

### 5.4 ④ 存储

- 规模 < 10 万节点、只做 1–2 跳扩展：**SQLite 两张表（nodes / edges）完全够用**
- 需要复杂路径查询、可视化：Neo4j
- 需要标准/推理/多源融合：RDF 存储 + 推理机

### 5.5 ⑤ 查询与推理

- 属性图：Cypher 路径查询是强项
- RDF：SPARQL 属性路径 + OWL 推理机（传递闭包、子类归并）
- 与向量检索融合见下一节

---

## 6. GraphRAG：图谱如何接入 RAG

按"投入从轻到重"排列：

### 6.1 混合检索（最轻，性价比最高）

```
向量检索 top-k  ─┐
                 ├→ 合并、去重 → 重排 → context → LLM
图检索（子图）  ─┘
```

流程：查询 → 识别实体 → 在图里取 1–2 跳子图 → 序列化为文本（"实体 A —[关系]→ 实体 B"）→ 与向量检索结果一起喂给 LLM。

- 优点：改动小、可增量上、可与现有评估体系对比
- 适用：实体关系类问题（"X 和 Y 什么关系""依赖哪些东西"）

### 6.2 微软 GraphRAG（重，偏全局总结）

流程：文档 → 分块 → LLM 抽取实体与关系 → 建图 → **Leiden 社区划分** → 为每个社区生成 LLM 摘要 → 查询时

- **全局模式**：用社区摘要回答"整体讲了什么主题"类问题
- **局部模式**：围绕命中实体展开子图回答具体问题

- 优点：解决向量 RAG 最弱的"全局总结"与"多跳"
- 代价：索引阶段每块多次 LLM 调用，成本高；增量更新不友好

### 6.3 LightRAG（中等，增量友好）

双层检索：**低层**查实体与关系，**高层**查主题与关键词；图与向量并存，支持增量插入。

### 6.4 LazyGraphRAG（轻索引，查询时推理）

索引成本与普通向量 RAG 相当，把 LLM 摘要等重活推迟到查询时。适合语料常变、预算有限的场景。

### 6.5 三者对比（粗略）

| 形态 | 索引成本 | 查询延迟 | 全局问答 | 多跳 | 增量更新 |
| --- | --- | --- | --- | --- | --- |
| 混合检索（6.1） | 低 | 低 | 弱 | 中 | 易 |
| GraphRAG（6.2） | 高 | 中 | 强 | 强 | 难 |
| LightRAG（6.3） | 中 | 中 | 中 | 强 | 易 |
| LazyGraphRAG（6.4） | 低 | 高 | 中 | 中 | 易 |

**共同点**：图在 RAG 里的定位不是替代向量检索，而是补齐**多跳、全局、可解释路径**这三类向量检索的短板。用图之前先确认你的问题里确实有这三类。

---

## 7. 和 kb-agent 的关系

> 本节对应 [PLAN v4](../PLAN.md) 的 L2（知识结构）；具体排期以 PLAN 为准。

### 7.1 现状

```
写入：loaders → cleaner → chunker(子chunk挂doc_id) → 嵌入 → Chroma(子) + SQLite(父)
读取：意图识别 → 改写 → 检索top-N → 重排top-k → 父文档扩写 → 编号引用 → LLM
```

当前是纯向量检索，没有实体/关系层。`Chroma` 存子 chunk，`SQLite` 存父文档全文。

### 7.2 理论上可插入的位置

| 阶段 | 可能的插入点 | 作用 |
| --- | --- | --- |
| 写入 | chunker 之后、indexer 之前 | 从每个 chunk 抽实体/关系，写 `nodes` / `edges` 表 |
| 读取 | retriever 之后、parent_expander 之前 | 实体链接 → 取子图 → 作为额外引用块参与编号与生成 |

注意：实体链接需要**查询 → 实体**的映射，这本身就需要一次抽取或检索，不能省。

### 7.3 若未来尝试，建议的验证顺序

1. 定一个 YAML "本体-lite"（10 行以内）
2. 对现有 `examples/sample_notes` 跑 LLM 抽取，**人工抽查 20 条**，看类型准确率与消歧质量
3. 只做"子图扩写"这一件事，接入现有评估（`recall@k` / `citation_accuracy` / `faithfulness`），与纯向量基线做 A/B
4. 确认多跳类问题的收益后，再考虑社区摘要之类的重方案

### 7.4 适配度自查

kb-agent 的示例语料（RAG 基础、向量检索、Python 异步、健康饮食）是**概念解释型**，向量 RAG 本身表现就不错，图谱增益有限。

图谱真正发力的是**实体密集型**语料：

- 人名、项目、团队、依赖关系
- 事件时间线与因果关系
- 引用网络、代码/模块依赖
- 大量需要"整体梳理"的文档集合

自查问题：你的真实问题里，"X 和 Y 什么关系""依赖哪几个东西""这批文档整体讲了什么"占比有多高？占比低，就不值得上图。

---

## 8. 成本、收益与常见坑

### 8.1 成本

- **抽取成本**：LLM 逐块抽取，调用量 ≈ chunk 数 × 1
- **消歧成本**：模型解决不了的部分需要人工规则/抽检
- **维护成本**：语料更新后图谱要同步增量维护
- **调试成本**：图谱错误比向量检索错误更难定位（"为什么这个实体没连上"）

### 8.2 收益

- 多跳问题（向量检索天然弱）
- 全局/主题级总结
- 可解释的路径（"答案来自 A→B→C 这条链"）
- 结构化过滤（按实体类型/关系精确筛选）

### 8.3 常见坑

| 坑 | 表现 | 对策 |
| --- | --- | --- |
| 过度建模 | 一上来写几百行 OWL，没人维护 | 本体-lite，按需扩展 |
| 混淆本体与分类法 | 用 SKOS 硬做领域 schema | 词表用 SKOS，领域模型用 RDFS/OWL |
| 把 LLM 抽取当真理 | 关系幻觉、重复实体 | evidence 校验 + 抽检 + 别名表 |
| 无界图扩展 | 1 跳变几十个节点，context 爆炸 | 限制跳数与节点数，按相关度裁剪 |
| 不评估就上线 | 感觉"更智能了"但指标没变 | 与纯向量基线做 A/B，看多跳子集 |
| 过早引入图数据库 | 维护成本远超收益 | 先 SQLite 两张表验证价值 |

---

## 9. 学习资源

### 标准与入门（免费、短）

- W3C **RDF 1.1 Primer**
- W3C **OWL 2 Primer**
- W3C **SPARQL 1.1** 规范与入门
- W3C **SHACL** 规范

### 动手

- **Protégé 官方 Pizza 教程**：经典本体建模练习，2–3 小时可完成
- **Neo4j GraphAcademy**：免费课程，Cypher 上手快
- **Apache Jena** 文档 + `riot` 命令行：玩 RDF/SPARQL

### GraphRAG / 图谱增强 RAG

- `microsoft/graphrag`（含论文与实现）
- `HKUDS/LightRAG`
- LazyGraphRAG（微软博客与论文）
- 相关综述：检索增强生成、图增强 RAG 方向

### 书

- 《知识图谱：方法、实践与应用》（王昊奋等）—— 中文较系统
- 《Ontology Engineering》（Obrst 等）—— 偏方法论

---

## 10. 术语速查

| 术语 | 英文 | 含义 |
| --- | --- | --- |
| 三元组 | triple | RDF 的基本单位 `(s, p, o)` |
| TBox / ABox | TBox / ABox | 术语层（类与关系）/ 断言层（具体事实） |
| 本体 | ontology | 对共享概念化的形式化、显式规范 |
| 公理 | axiom | 施加在模型上的逻辑陈述，如子类、传递性 |
| 开放世界假设 | OWA | 未断言 ≠ 假；本体推理的默认语义 |
| 封闭世界假设 | CWA | 未断言 = 假；数据库/校验的默认语义 |
| 实体链接 | entity linking | 把文本中的提及映射到图谱中的实体 |
| 实体消歧 | entity resolution | 判断两个提及/节点是否是同一实体 |
| 共指消解 | coreference | 代词/省略指代的指向判定 |
| 信息抽取 | IE | 从非结构化文本抽取结构化事实 |
| 属性图 | property graph | 节点与边均可带属性的图模型 |
| 社区检测 | community detection | 图聚类，如 Leiden、Louvain |
| 推理机 | reasoner | 按本体公理推导隐含知识的程序 |
| 属性路径 | property path | SPARQL 中沿关系多跳匹配的语法 |
