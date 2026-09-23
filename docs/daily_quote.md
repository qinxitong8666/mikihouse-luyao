# MIKI HOUSE 每日报价生成工具

## macOS 报价助手

仓库根目录的原生 AppKit 应用 `MIKI HOUSE 报价助手.app` 是现有 CLI 的可双击桌面外壳。它只负责展示阶段进度、最新汇率/商品数/PDF 大小和打开产物；生成动作仍由 `scripts/generate_daily_quote.py` 执行，双收藏动作仍由 `scripts/run_mikihouse_daily_production.py` 执行。应用不持有微信 Sink，不直接执行任何收藏或 Shijiu 操作，也没有绕过 `production_save_enabled`、精确 confirmation、checkpoint、幂等、防重复与强回读门禁的参数。

两个核心入口可用 `--progress-jsonl` 向 stderr 输出以 `MIKIHOUSE_PROGRESS ` 开头的单行 JSON 事件。该事件只增加可观察性，不参与 DailyQuoteManifest、定价、筛选或写入判定。默认跟踪配置下正式保存按钮为禁用状态。

## 边界与安全

当前主线只进行 MIKI HOUSE 官网只读抓取、ECB 汇率读取和本地文件生成。`config/daily_quote.json` 中 `shijiu_requests_enabled` 与 `wechat_write_enabled` 必须同时为 `false`，否则入口立即停止。每日模块不包含 `/shopapi/` endpoint，也不接受跳过该门禁的命令行参数。

历史 Shijiu 代码、mapping、checkpoint 与证据保留，不删除、不迁移、不调用。原有 `deliverables/mikihouse_2026AW_price_catalog.pdf` 和 351 特殊品番生产流水线也不由每日入口修改。

## 单次冻结数据流

```text
完整 Storefront crawl ─┐
                       ├─> DailyQuoteManifest ─> PDF
ECB/人工 FX 冻结 ──────┘                      ├─> 文字报价
351 权威清单 ──────────────────────────────────┼─> 两个 Favorite preview
主图内容 hash / thumbnail manifest ───────────┴─> 内部变化报告
```

`daily_quote_manifest.json` 是所有输出的唯一 Source of Truth。客户输出函数不含网络抓取能力。manifest 保存官网快照 hash、FX provider/rate/date/hash、折扣率、排除统计、逐商品内容 hash、逐有货 variant 的 SKU/颜色/尺码/JPY 源价/人民币整数价，以及内部的全部 variant availability 状态。

完整 crawl 必须通过：总数与归一化唯一商品数一致、无重复 `product_number`、高于配置的最低数量、相对上次成功 manifest 没有异常骤降。失败时 run 在发布阶段前停止，原 `last_successful_manifest.json` 不变。

## 汇率与价格

默认 provider 是 ECB `eurofxref-daily.xml`。因 ECB 公布的是每 1 EUR 对应的货币数量，工具按：

```text
JPY_TO_CNY = CNY_PER_EUR / JPY_PER_EUR
```

进行推导。汇率、折扣和 JPY 价格全部使用 `Decimal`，最终客户价使用 `ROUND_CEILING` 到整数人民币。超过 `fx_max_staleness_days`、缺少 CNY/JPY、未来日期、网络或 XML 错误都 fail closed；没有内置固定汇率回退。

同一商品的有货 variant 先逐项算价，再按最终 CNY 价格分组。颜色和尺码只在所属价格组内合并，避免“最低价”“起价”或规格错配。

## PDF 与检索验收

全集 PDF 使用 12 件/页的 A4 3×4 卡片，分类顺序固定为鞋类、婴幼儿、服装。索引按品番排序并链接到商品页；三个分类各有 outline/bookmark。每个品番使用 Helvetica 的真实 text object，商品名、颜色、尺码、价格使用嵌入的 Unicode 字体。

水印契约为 `株式会社路遥` / `opacity=0.18` / `scope=PRODUCT_PAGES_ONLY`。首页及所有品番索引页不绘制水印；全集和超限时生成的三个分类 PDF 都只在商品页绘制。自动验收会逐页校验索引页零水印、商品页水印文字完整，以及 PDF ExtGState 的实际 fill alpha 为 0.18；任一页不符合都使生成任务 fail closed。

主图在嵌入前经过独立缓存处理：EXIF transpose、透明层白底 flatten、RGB/sRGB 兼容输出、长边 360px、默认 JPEG quality 70、按内容 hash 去重。源高清图不直接嵌入客户 PDF。

自动验收至少从首页、末页、分类边界和固定种子随机样本抽取 50 个品番，使用 `pypdf` 对完整文本层精确检索；任一缺失、重复落页、outline 缺失或水印 scope/alpha 不匹配都会使 PDF 任务失败。视觉改动还必须把全部页面以至少 200dpi 渲染并人工检查后，才能把 `visual_qa.status` 标记为 `PASS`。

## 微信收藏 preview

每天固定准备两个 payload：

1. PDF 版：全集不超过 50MB 时附全集；超过时附三个分类 PDF；
2. 文字版：默认不显示商品名，按分类输出品番、人民币价、颜色与当日有货尺码。

对 `qinxitong8666/luyao-quote-assistant` 的只读参考只复用通用 Favorite Sink 的页面识别、分段写入、累计回读和 fail-closed 思路，不复制其报价业务。真实 Mac 微信验收的容量、PDF 附件与重开回读结果以当日 `wechat_*_runtime_*.json` 为准；实现契约见 [`wechat_favorite_runtime.md`](wechat_favorite_runtime.md)。正式保存仍受独立默认关闭的双重开关限制。只读参考证据见 [`luyao_quote_assistant_readonly_reference.md`](luyao_quote_assistant_readonly_reference.md)。

2026-09-22 运行时样例中，PDF 收藏实测 PASS。文字收藏固定使用 `LOSSLESS_COMPACT`：70,661 字、96,870 UTF-8 bytes、1,794 行、1,785 件商品，保留每件品番、人民币价、颜色和有货规格。真实微信笔记已完成分块写入、保存关闭、唯一搜索重开和完整哈希回读，无截断。每天恰好两个收藏的 runtime readiness 已 PASS。正式双收藏编排器现已接入每日生成主线，但默认生产写入开关仍关闭；普通入口不会自动写入微信，明确授权的生产入口也必须先完成 fresh manifest、PDF、容量、标题冲突和 checkpoint preflight。

## 命令

正常在线运行：

```bash
PYTHONPATH=src python scripts/generate_daily_quote.py
```

人工冻结 FX（只用于离线测试或明确的紧急人工运行）：

```bash
PYTHONPATH=src python scripts/generate_daily_quote.py --fx-rate 0.048
```

使用已证明完整的离线 source snapshot：

```bash
PYTHONPATH=src python scripts/generate_daily_quote.py \
  --source-snapshot outputs/daily_quote/2026-09-22/source_snapshot.json.gz \
  --fx-rate 0.048
```

统一离线验收：

```bash
python scripts/verify_local.py
```

真实官网运行与 FX 读取需要网络，但不会访问 Shijiu。运行结果中的 `daily_quote_stats.json` 必须记录 `shijiu_request_count=0`、`shijiu_mutation_count=0`、`writer_mutex_evidence_count=0`、`wechat_real_write_count=0`。
