# AGENTS.md

本文件约束 Codex 在 `mikihouse-luyao` 仓库中的开发、验收、提交与交付方式。除非 Issue 明确授权更高风险行为，以下规则均为默认强制要求。

## 1. 仓库定位

本仓库同时包含四类链路：

1. MIKI HOUSE 官网单品抓取与 PDF 商品册；
2. Storefront 全站商品主库与增量变化；
3. Shijiu 导入 planning / dry-run / contract 审计；
4. 具有真实下游写入能力的 Shijiu live import / recovery / browser-exact capture。

不得把这四类链路混成一个“抓商品脚本”。修改某一层时，必须保持其他层的稳定契约。

## 2. 稳定业务标识与数据不变量

以下语义属于长期不变量，除非 Issue 明确要求迁移并附完整回归方案，否则不得改变：

- 商品稳定标识：`product_number`；
- variant 稳定标识：`product_number::variant SKU`；
- variant SKU、颜色、尺码、价格、库存必须保持逐 variant 语义；
- `special_skus_2026aw.csv` 是 PDF 专用池，同时是 Shijiu 永久排除集合；
- 特殊品番不得因为官网重新上架、增量同步、性能优化或恢复逻辑而重新进入 Shijiu 候选；
- Shijiu 的唯一正式上游池是 `stable_catalog`，不是“官网全站减特殊品番”的旧候选池；
- 官网明确 `WEB限定/WebLimited/WEB LIMITED` 商品必须以 `WEB_EXCLUSIVE` 前置排除；
- `compareAtPrice > price` 或明确限时/促销价格商品必须以 `LIMITED_TIME_PRICE` 前置排除；
- 无法可靠判定上述两类状态的商品必须进入 `REVIEW_REQUIRED_STABILITY`，不得生成任何 Shijiu 动作；
- PDF特殊、WEB限定、促销价格及稳定性复核过滤必须发生在所有 Shijiu CREATE/UPDATE/库存/图片/价格/下架/恢复 planning 之前；
- active / inactive / restored 的语义不得用“本次请求没看到”代替；
- 官网抓取失败、分页不完整、网络失败、响应结构异常不得静默解释为下架；
- 只有全站采集完整成功并通过校验后，才能提交新的主库/增量状态；
- 价格 guard、分类映射、SKU 去重和失败隔离不得被 UI/PDF/性能改动顺手改变。

## 3. Storefront / supplier adapter 规则

对 `scraper.py`、`catalog.py`、Storefront API 或未来供应商 adapter 的修改：

- 必须保持分页完整性和 variant 分页完整性；
- 必须显式处理重复 product/variant；
- 不得依赖列表顺序作为稳定 ID；
- 不得把 supplier-specific 原始字段泄漏成核心稳定标识；
- 必须保持失败隔离与 fail-closed；
- 需要改数据模型时，先说明旧数据兼容策略；
- 增量同步必须可重复执行，不得因重跑产生重复商品、重复 variant 或错误下架；
- 对恢复上架必须保留历史稳定 ID。

## 4. Shijiu planning 与 live write 分层

### 4.1 默认允许的只读/离线行为

普通开发与统一验收可以：

- 运行离线 pytest；
- 解析本地 fixture/config/state；
- 生成 dry-run / planning 数据到临时目录；
- 做 contract/schema 静态检查；
- 做代码级 browser helper 语法检查。

### 4.2 默认禁止的真实写入

除非当前 Issue/任务明确授权，否则禁止执行：

- Shijiu CREATE；
- Shijiu UPDATE；
- 图片上传；
- 删除；
- 下架；
- 恢复上架；
- 任何会改变生产 Shijiu 商品状态的调用。

`$issue-to-verified-push`、`scripts/verify_local.py`、GitHub Actions CI 都不得触发上述行为。

### 4.3 live write 明确授权要求

若任务确实要求真实 Shijiu 写入，必须同时满足：

1. Issue/用户明确要求 live write；
2. 显式 write confirmation gate 生效；
3. 写前完成 read-only discovery / duplicate risk / contract 校验；
4. 每次写入后做 readback；
5. 失败或不确定时立即 fail-closed，禁止盲重试；
6. 保存必要 forensics/evidence，但必须脱敏；
7. 不得把 token、secret、cookie、authorization 写入 Git、PR、Issue 或日志。

## 5. 持久状态与正式交付物保护

以下内容属于受保护状态：

- `state/**`；
- `deliverables/**`；
- 正式 Storefront master catalog / production output（若本地存在）；
- Shijiu mapping / checkpoint / recovery state。

普通测试、DevEx、文档改动、统一验收不得重建、清空或覆盖它们。

如果任务需要修改这些内容：

- 必须明确说明原因；
- 先做备份/旧值快照；
- 使用原子写入；
- 在 PR evidence 中记录受影响文件与验证结果；
- 不得把临时抓取产物误提交成正式状态。

## 6. PDF / 视觉输出规则

PDF 或图片布局改动不能只靠单元测试判定完成。

适用时必须额外完成：

- PDF render；
- 页面数量和商品数量校验；
- 至少 200dpi 的人工视觉检查；
- 确认客户版不泄漏内部定价字段；
- 图片、颜色、尺码、价格对应关系人工抽查。

未做人工视觉验收时不得把 visual QA 写成 PASS。

## 7. 分级验收

### Level A：默认离线验收

从 repo root 运行：

```bash
python scripts/verify_local.py
```

这是所有代码/文档 Issue 的基础验收入口。

### Level B：真实官网 read-only smoke

若修改以下区域，除 Level A 外还应按 Issue 要求执行真实官网只读 smoke：

- `src/mikihouse_luyao/scraper.py`
- `src/mikihouse_luyao/catalog.py`
- Storefront contract / pagination / variant mapping
- 在线图片或商品字段解析

真实网络 smoke 结果必须单独记录；网络未执行时写 `NOT CAPTURED` 或 `NOT APPLICABLE`，不得从离线 pytest 推断在线成功。

### Level C：Shijiu planning / dry-run

若修改：

- `shijiu_import.py`
- Shijiu category mapping
- payload planning / contract / price guard

必须验证 dry-run / contract 行为，但默认不得 live write。

### Level D：Shijiu live / recovery / browser-exact

若修改：

- `shijiu_live_import.py`
- `shijiu_recovery.py`
- live probe / browser-exact capture

离线测试仍是基础要求；真实 Shijiu 验收只有在用户明确授权后才能运行。

## 8. Definition of Done

任务只有同时满足以下条件才可称为完成：

1. 开始前审计现有实现，避免重复建设；
2. 实际修改范围与 Issue/Phase 一致；
3. 适用测试全部 PASS；
4. `python scripts/verify_local.py` PASS；
5. 适用的在线 smoke / PDF visual QA / Shijiu dry-run 或 live evidence 已真实执行或明确标记未执行；
6. 未意外修改受保护 state/deliverables；
7. 无秘密、debug dump、无关大文件或临时产物进入 diff；
8. `git diff --check` PASS；
9. local HEAD SHA 与 remote feature branch SHA 一致；
10. GitHub Issue feature branch 工作有 PR evidence；
11. 若仓库 CI 适用，最终 CI 必须完成并成功后才能写 `COMPLETED`；
12. 最终报告明确列出改动、测试、风险、线上/视觉/写入状态。

不得用“基本完成”“应该可以”“代码层面完成”掩盖适用验收失败或缺失。

## 9. Git 安全规则

禁止：

- `git reset --hard`；
- `git clean -fd`；
- force push；
- 为制造“完成感”创建空 commit；
- 删除或覆盖用户未提交工作；
- 未经授权直接在 `main` 上开发 Issue。

开始任务前先检查：

```bash
git status -sb
git branch --show-current
git remote -v
```

若工作区已有用户改动，必须保留并避免混入任务 commit。

## 10. Commit / Push / Remote Verification

提交前至少执行：

```bash
git status -sb
git diff --check
git diff --cached --check
git log -1 --oneline
```

推送后必须验证：

```bash
git rev-parse HEAD
git push <remote> HEAD:<target-branch>
git ls-remote <remote> refs/heads/<target-branch>
```

必须证明 local HEAD SHA == remote target SHA。

`Everything up-to-date` 单独不能证明任务已正确完成。

## 11. PR 验收证据

GitHub Issue 驱动、feature branch 交付的任务必须创建或更新 PR，禁止重复创建相同 head/base PR。

PR evidence 至少包含：

- Issue；
- Status；
- branch；
- commit；
- local SHA；
- remote SHA；
- Remote verified；
- unified verification；
- pytest；
- config / Node syntax；
- online Storefront smoke（适用时）；
- Shijiu dry-run / live 状态（适用时）；
- PDF visual QA（适用时）；
- protected state/deliverables unchanged；
- CI final status；
- working tree；
- risks/follow-ups。

未实际看到的证据必须写 `NOT CAPTURED`，不得推断 PASS。

CI 仍在 queued/in_progress 时，状态只能写 `AWAITING_CI` / `READY FOR REVIEW` / `PARTIAL`，不能写 `COMPLETED`。

CI 成功后应更新 PR evidence 和 Issue 索引到最终状态。

## 12. Issue 索引

若 Issue 可写，PR 创建/更新后留下简短索引：

```text
PR: #<pr>
Branch: <branch>
Verified remote SHA: <sha>
Status: <status>
```

CI 最终完成后同步更新该索引，不要让 Issue 永久停在 `awaiting CI`。

## 13. Skill 使用

显式使用：

```text
$issue-to-verified-push 完成 Issue #<number>
```

该 Skill 不允许自动 merge。merge 是独立审查决定。

## 14. SHIJIU 来源所有权与生产写入互斥

MIKIHOUSE 是独立 source ownership 域。

### 14.1 来源所有权

- 只能修改 `state`/mapping 已明确证明属于 MIKIHOUSE 的 backend product/SKU IDs，或当前单轮明确授权 CREATE 后由稳定 MIKI identity 完成回读绑定的实体；
- 不采用 `WAWU-*` SKU，不得修改 WAWU registry/binding 已管理的商品；
- 不得按商品名、列表位置、全店商品总数、类目、创建时间或近似匹配认领商品；
- MikiHouse 类目 `294884` 本身不是 ownership proof，类目内 foreign/legacy 对象不得自动接管；
- 任何归属不明确实体一律 `FAIL_CLOSED_NO_WRITE`；
- 全店商品总数只能作为观察值，不能作为 MIKIHOUSE 同步健康的固定不变量。

### 14.2 全局 SHIJIU production write mutex

多个项目可以并行开发、测试、官网抓取和只读审计，但同一个 SHIJIU 正式租户在同一时间只能有一个项目执行业务写入。

真实 CREATE/UPDATE、库存/上下架、价格、weight、生产图片上传及会改变正式店铺状态的调用都属于 production write window。

开始写入前必须有任务级明确授权，并记录 writer=`MIKIHOUSE`、仓库/分支/SHA、精确写入范围、开始/停止条件。若不能证明没有其他项目正在写 SHIJIU，则禁止开始 live write。

生产写入证据必须包含：

- `shijiu_writer_source=MIKIHOUSE`；
- 所有 UPDATE/DELETE 目标的 MIKIHOUSE ownership proof；
- `cross_source_writes=0`；
- `concurrent_shijiu_writer_observed=false`；
- production write window 开始/结束时间；
- CREATE/UPDATE/upload/readback/failure/transport-unknown 计数。

任一跨来源写入或并发 production writer 重叠，都不得报告 READY/COMPLETED。
