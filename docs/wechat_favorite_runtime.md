# Mac 微信收藏运行契约

## 边界

该链路只消费已冻结的 `DailyQuoteManifest` 及其两个 Favorite payload，不抓官网、不重算价格、不访问 Shijiu。目标进程必须唯一匹配：

- bundle id: `com.tencent.xinWeChot2`
- app path: `/Applications/微信2.app`

所有测试收藏标题必须以 `MIKIHOUSE_TEST` 开头。运行器只允许新建笔记，不进入聊天、不发送、不修改既有收藏、不自动删除测试笔记。页面或目标不唯一时 fail closed。

## 强回读

文字写入按字符分块，每块后全选回读并比较累计内容；保存后关闭笔记，在收藏页使用唯一测试标记搜索并重新打开。证据保存：

- Unicode 字符数、UTF-8 bytes、行数；
- 完整 SHA-256；
- 首部/尾部 SHA-256；
- 是否截断；
- 页面指纹及重开证据。

PDF 收藏额外验证重开后的标题、正文和附件文件名。容量探测按 10,000、30,000、60,000、90,000 和完整正文递增，首次失败即停止。

## 2026-09-22 真实验收

- 微信2 `4.1.6`，精确 bundle/path 命中；
- 10,000 字符：3 段写入，保存关闭后唯一搜索重开，完整归一化 SHA-256 一致；
- 30,000 字符：8 段写入，保存关闭后唯一搜索重开，完整归一化 SHA-256 一致；
- 60,000 字符：初次运行时已强回读证明 51,649 个正文字符前缀，之后一次新增分段写入后连续无法强回读；未重发 mutation，立即停止；
- 90,000 与 104,006：`NOT_TESTED_STOP_ON_FIRST_FAILURE`；
- PDF：实际文件 23,280,126 bytes，微信界面显示 22.20MB，同步完成后唯一搜索重开，标题、正文、附件文件名均存在，`PASS`。

因完整 104,006 字符没有通过真实运行时验收，两条收藏的整体生产 readiness 为 `BLOCKED_TEXT_CAPACITY_NOT_FULLY_VERIFIED`。无损紧凑实验将 104,006 字符降到 70,661（-32.06%），但本轮没有将它替换为生产格式，也没有在首次失败后继续写入测试。

### 60,000 阶梯的后续纯只读诊断

精确标题搜索能唯一重开该笔记。重开后笔记共 55,580 个原始字符、75,966 UTF-8 bytes；去除标记并归一化换行后，正文是原文精确的 55,544 字前缀，哈希一致，但没有达到 60,000。因此：

- 不能从这条笔记推断微信的实际存储上限；
- 已排除 51,649 字或 75,966 bytes 以下的硬剪贴板上限；
- 笔记正文未通过 Accessibility tree 直接暴露，强回读仍依赖 `Cmd+A/Cmd+C`；
- 旧 0.2 秒选区等待失败，1.5 秒等待后可重复复制同一哈希，编辑器渲染/选区时序是主要嫌疑项；
- 微信“软件更新”窗口被旧解析器误认成第二个笔记窗口，是另一个窗口识别假失败源。

用户授权的先决条件是“确认 60,000 字完整保存，且仅为旧回读方法问题”。该条件不成立，所以未修改现有收藏、未新建 70,661 字紧凑版测试收藏，生产开关仍关闭。机器证据见 `wechat_runtime_readback_diagnosis.json`。

### 新笔记的最终容量验收

后续明确授权后，新建 `MIKIHOUSE_TEST_2026-09-22_COMPACT_70661` 测试笔记，仅写入无损紧凑文字版。该正文为 70,661 字、96,870 UTF-8 bytes、1,794 行，包含 1,785 件商品。验收完成：

- 分块写入，每个 mutation 只发送一次；
- 临时回读不稳定时只重试 `Cmd+A/Cmd+C` 纯只读验证，不重贴正文；
- 保存关闭后以完整标题搜索，候选数为 1；
- 重开后完整归一化 SHA-256、首部 SHA-256、尾部 SHA-256、字符数和行数全部一致，无截断。

因此生产文字收藏格式固定为 `LOSSLESS_COMPACT`，“PDF版 + 文字版”恰好两条收藏的运行能力状态为 `PRODUCTION_READY_TWO_FAVORITES`。这不会自动打开正式写入；`production_save_enabled` 仍为 `false`。详细证据见 `wechat_compact_text_runtime_validation.json`。

## 开关

`config/wechat_favorite_runtime.json` 是独立的运行时门禁，不改动每日报价业务配置。正式写入必须同时满足：

1. `runtime_validation_status=PASS`；
2. payload 的 manifest hash 与已验证 hash 一致；
3. `production_save_enabled=true`，或由 `MIKI HOUSE 报价助手.app` 创建并由 runner 原子消费有效的一次性许可；
4. 命令行显式传入 `--production-save`；
5. 显式传入精确 confirmation phrase。

默认 `production_save_enabled=false`。App 不修改该配置；每次用户在 App 内勾选明确确认后，许可只在 `.secrets` 私有目录保存，绑定仓库路径与当前 HEAD，15分钟过期、权限 `0600`、首次消费后不能复用。直接运行 CLI 且没有该许可时仍在官网 crawl 前 fail closed。

## 正式每日双收藏编排

`scripts/run_mikihouse_daily_production.py` 是生成与微信保存的唯一正式组合入口。它不会消费任意旧 preview：每次先运行完整官网 crawl、冻结一次 FX、生成同一份 `DailyQuoteManifest`、PDF 和 `LOSSLESS_COMPACT` 文字 payload，然后进行零写入 preflight：

- manifest 自哈希、日期目录与两个 payload 的 manifest hash 必须完全一致；
- 当日生成阶段必须记录两个 preview 且微信/Shijiu/mutex 写入计数均为 0；
- PDF 自动检索/结构验收必须 PASS；
- PDF 附件必须存在并与单全集/三分类策略一致；
- 文字格式固定为 `LOSSLESS_COMPACT`，逐商品品番不得遗漏，字符数/UTF-8 bytes/行数不得超过真实保存重开验证过的 70,661 / 96,870 / 1,794；
- tracked runtime evidence 必须仍为 `PRODUCTION_READY_TWO_FAVORITES`；
- 当前 payload 只通过 invocation-time fresh manifest hash 绑定进入 Sink，历史 2026-09-22 manifest 只作为能力验收样本，不会被误当成每日 payload。

写入顺序固定为 PDF→文字。`wechat_daily_production_checkpoint.json` 在每次 mutation 前原子落盘；成功 stage 的 payload hash 与回读 evidence 被持久记录。若进程在两条之间正常中断，可在 bundle 完全未变时跳过已 PASS 的 PDF；若任一 mutation 已发送后返回异常或回读不一致，checkpoint 标记 `FROZEN_RECONCILIATION_REQUIRED`，后续运行 fail closed，不自动重发 mutation，也不继续下一条。整轮 PASS 后重复运行只返回幂等结果，微信 mutation 为 0。

### PDF 附件文件选择器恢复契约

2026-09-24 用户重启后的真实测试已证明：Right 单键不足以解除选区；`Command+Down → Command+Right` 才能稳定移至正文末尾，随后 `Command+O` 打开笔记自己的 `open-panel`。测试收藏 `MIKIHOUSE_TEST_2026-09-24_KEYBOARD_END` 已实际插入 23,317,467 bytes PDF，保存、重新搜索打开后正文 normalized hash 相同，只有一个尾随 `[文件]`，屏幕显示正确文件名和 22.24MB。

原生 AX 适配器 `wechat_native_picker.py` 只有限遍历该笔记的文件 sheet，不扫描整个微信进程。GoToWindow/PathTextField 只用于导航父目录，最终必须按完整 AXURL 匹配一个文件且具有 AXOpen。注意系统实际给的是 `file:///.file/id=...` 文件引用，必须用 CFURLCreateFilePathURL 解析；不能用 AXFilename 同名匹配替代完整路径。该解析器和 GoToWindow 所有权已在真实面板只读验收。

重开后完整正文与尾随附件标记仍是必要条件，另以完整附件文件名搜索收藏索引并匹配同一唯一标题；正文中若含该文件名，不能使用这个索引证明。因为编辑器 AX 不提供附件卡片名称，不能把 AX 缺字段直接当附件丢失，也不能仅凭 `[文件]` 放行。重新搜索会先清空旧查询，避免保留保存前的“无结果”。

历史 `MIKIHOUSE_TEST_2026-09-24_NATIVE_AX_RUNNER` 在 picker 确认前冻结，未上传，继续保留且不重试。旧异常没有记录具体返回码，无法把原始失败唯一归因于窗口标题/时序。新的实际测试确认并修复以下差异：

- Cmd+Down/Right 分步稳定光标，再使用物理 O 键；只发送一次快捷键。面板安装后每次只读轮询重新获取 native AX 对象，绑定唯一前台笔记的 `AXChildren → open-panel`，不持有旧 System Events window proxy 跨模态转换。
- GoToWindow 的路径字段使用它自己的 `AXFocused`，不要求微信主进程提供实际缺失的 `AXFocusedUIElement`。
- 实际 AXOpen 返回 `-25205` 时，PDF 已插入：该返回值本身不是成功，只允许继续回读。必须证明面板关闭、完整正文不变及唯一尾随附件；保存重开和文件名索引仍必须通过。其它未验证的错误立即冻结，不重发任何文件动作。
- PDF 保存使用当前唯一笔记自身 `AXCloseButton/AXPress`，不把主进程/笔记辅助进程不同的“文件→关闭”菜单混用。
- 搜索后焦点原本留在搜索框，Down/Return 不能稳定重开。现在精确查询下必须只有一个笔记结果，显式聚焦 `fav_detail_list`，只接受该列表或它的唯一结果子项 `AXFocused=true`，再 Home/Return；后者是实测的微信焦点转交行为，不是放宽到任意焦点。

独立 V2、V3 测试各仅上传一次；失败后只完成剩余只读取证，不算完整程序通过、不自动重传。最终全新 `MIKIHOUSE_TEST_2026-09-24_SINK_AX_V4` **同一未中断 Sink 调用 PASS**，精确正文 hash、末尾附件、唯一标题重开和完整文件名均通过。因此额外 `coordinate_free_pdf_runtime_validation_status=PASS`，App 正式保存和冻结附件恢复复用此 Sink；默认 `production_save_enabled=false`、单次授权、collision/checkpoint 门禁仍有效。本轮未操作正式收藏。证据：[完整 Sink 运行](evidence/wechat_pdf_full_sink_runtime_20260924.json)。V2/V3/V4 测试笔记都保留，后续清理需独立授权。

2026-09-23 真实生产证明，微信 4.1.6 可能不接受剪贴板 file-alias 粘贴，但同一草稿中的工具栏「文件」选择器能正确附加 PDF。该路径已固定为 `OPERATOR_AUTHORIZED_TOOLBAR_FILE_PICKER_SINGLE_ATTEMPT`，不是通用自动 retry：

- 原附件写入已发送且不可见时必须立即冻结；
- 必须取得当轮人工明确授权，只能复用同一已打开草稿，不得新建笔记；
- 选择器必须使用 payload 中经解析的精确绝对路径，并校验文件名、字节数和 SHA-256；
- 保存前必须看到附件；保存后必须精确标题唯一搜索、重开，验证正文和附件文件名；
- 单次恢复任一环节不确定时继续 fail closed，禁止自动重试、重新创建或继续文字收藏。

`config/wechat_favorite_runtime.json` 仅声明上述 fail-closed 契约，不会自动打开选择器或开启正式写入。当日恢复证据经纯本地函数 `validate_pdf_file_picker_recovery_evidence` 校验；该函数不调用微信 GUI。

`TOOLBAR_FILE_PICKER_SINGLE_ATTEMPT` 仅保留为历史 checkpoint 的兼容策略标识，不再代表坐标点击。实现为上述 Cmd+O + native AXURL/AXOpen；缺少完整 runner PASS 时仍在任何正式 UI 动作前禁止写入，App 单次授权不能绕过。任何不确定都冻结，不回退到剪贴板或旧坐标路径，不自动重发文件选择。App 通过仓库入口加载当前 Sink，无需另建一套 GUI 自动化；既有已冻结 checkpoint 不会因能力验收 PASS 自动解冻。

对已经出现的“文本已保存、附件缺失”状态，恢复入口另有 operation-bound App 许可：只读标题必须为 `[PDF=1, TEXT=0]`，原 checkpoint 必须是第一次附件不可见错误，且文字阶段从未 mutation。恢复只补现有 PDF 收藏，不创建第二条 PDF 收藏；成功后 checkpoint 才把 PDF 计为 1 并继续唯一待处理的文字收藏。

生产 Sink 在创建前使用与重开相同的收藏页精确标题搜索做只读 collision preflight；任何既有/歧义候选都禁止新建。每条保存后强回读并关闭已验证笔记窗口，下一条从干净窗口状态开始。全过程不进入聊天、不发送、不修改/删除既有收藏、不访问 Shijiu。

默认安全状态：

- `config/wechat_favorite_runtime.json` 的 `production_save_enabled=false`；
- 普通 `scripts/生成MIKIHOUSE每日报价.command` 和 GUI“生成今日报价”永远 preview-only；
- 旧 `scripts/生成并保存MIKIHOUSE每日两个微信收藏.command` 在默认配置下继续 fail closed；
- `MIKI HOUSE 报价助手.app` 通过清晰的单次确认签发私有一次性许可，无需手工修改配置；
- 正式 runner 仍必须收到 production mode、内部精确 confirmation 和有效许可；任一不匹配时在官网 crawl 前停止，微信写入为 0。

### 当天收藏被人工删除后的安全重建

- 只允许重建当天、已有 `PASS` checkpoint/report 且原记录证明恰好两条收藏的生产日；
- App 在授权前通过真实微信收藏页只读精确搜索 PDF版和文字版标题；任一标题候选数不为 0 即禁止重建；
- 重建使用独立 `REBUILD_MISSING_DAILY_TWO_FAVORITES` 单次许可，不能用普通首次生产许可代替；
- fresh crawl 和当日 bundle 验收后，runner 再只读搜索两个标题。只有第二次检查仍为 `[0,0]` 时，才归档旧 checkpoint/report/evidence 并原子重置 checkpoint；
- 重建后仍按 PDF→文字、逐阶段 checkpoint、mutation 不重试、标题冲突再防护和强回读执行。

CLI（当前默认会 fail closed，不会写微信）：

```bash
PYTHONPATH=src python scripts/run_mikihouse_daily_production.py \
  --production-save \
  --confirm CONFIRM_MIKIHOUSE_WECHAT_FAVORITE_PRODUCTION_SAVE
```

仓库不提供跳过 runtime evidence、容量、manifest、collision、checkpoint 或回读门禁的参数。

## 最终零写入验收

正式启用前可运行：

```bash
PYTHONPATH=src python scripts/verify_wechat_daily_production_acceptance.py
```

该验收器强制要求 tracked `production_save_enabled=false`，否则自身 fail closed。它只对已验收样例执行本地 bundle preflight，调用真实一键 CLI 证明默认门禁在官网 crawl 前停止，并用隔离临时目录/Fake Sink 验证：PDF→文字顺序、逐 stage checkpoint、PDF 已 PASS 后只续跑文字、整轮 PASS 后幂等重跑零创建、bundle 变化拒绝、标题碰撞在新建前拒绝，以及 mutation 结果不确定时冻结且不自动重试。不会启动微信 GUI、不会创建/修改收藏、不会访问 Shijiu。

机器结果写入 [`docs/evidence/wechat_daily_production_final_acceptance.json`](evidence/wechat_daily_production_final_acceptance.json)。`PASS_FINAL_ACCEPTANCE_PRODUCTION_GATE_OFF` 只表示正式流程具备受控启用条件，不等于生产开关已开启，也不授权真实保存。

## 命令

生成无写入的压缩实验和容量计划：

```bash
PYTHONPATH=src python scripts/prepare_wechat_runtime_audit.py \
  --daily-dir outputs/daily_quote/2026-09-22
```

单条明确标识的 runtime test：

```bash
PYTHONPATH=src python scripts/save_wechat_daily_quote_favorite.py \
  --daily-dir outputs/daily_quote/2026-09-22 \
  --kind text --runtime-test
```

正式模式的双重开关默认关闭，本文档不提供任何跳过方式。

## 收藏页识别修复（2026-09-23）

当日 App 的附件恢复在只读标题核查阶段出现 `favorites page strong fingerprint failed`。
AX 采集改为在 `System Events` 作用域内显式读取 AXRole/AXTitle/AXDescription/AXValue，
避免控件属性变成空值。收藏搜索结果以实际笔记卡片计数，不把搜索框、结果标题或选中列表的名称当成收藏。
相同标题的两个卡片仍算两个；只有列表标题却没有可读卡片、或 AX 读取不完整时，禁止认定收藏不存在。
候选标题仍不是完整身份凭据，打开后的正文必须继续强回读。

本次已只读观察到当天 PDF 版的一个卡片和缺附件的笔记；打开文件选择器检查后已取消，
没有选择文件、上传附件或创建新收藏。上述识别修复不等于附件恢复成功，原冻结 checkpoint 保持不变。
