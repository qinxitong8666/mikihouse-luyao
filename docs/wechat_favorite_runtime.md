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

## 开关

`config/wechat_favorite_runtime.json` 是独立的运行时门禁，不改动每日报价业务配置。正式写入必须同时满足：

1. `runtime_validation_status=PASS`；
2. payload 的 manifest hash 与已验证 hash 一致；
3. `production_save_enabled=true`；
4. 命令行显式传入 `--production-save`；
5. 显式传入精确 confirmation phrase。

默认 `production_save_enabled=false`。运行验收即使 PASS，也不会自动保存后续每日收藏。

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
