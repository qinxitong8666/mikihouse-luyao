# mikihouse-luyao

MIKI HOUSE 商品抓取与微信客户版 PDF 商品册。程序读取 `special_skus.csv`，通过官网 Storefront API 按品番抓取并校验商品名、税入价、官方高清主图、颜色、尺码和库存；随后缓存主图、输出底层 JSON，并制作每页最多四件的 A4 商品卡片。

## 定价规则

```text
PDF售价 = ceil(税入价 × 0.73 × 0.0435)
```

公式逐变体计算。若同一商品的颜色或尺码价格不同，每个变体分别保存 `tax_included_price_jpy` 和 `pdf_price`，不会再因价格不一致而拒绝整个商品。例如 `10-1105-495` 的税入价为 `¥44,000`，人民币售价为 `1398`。

为兼容旧数据读取方，商品级 `tax_included_price_jpy` 和 `pdf_price` 在所有变体同价时仍保留数值；多价商品中这两个字段为 `null`。JSON 同时提供商品级的 `*_min`/`*_max` 汇总字段，实际销售价应始终以 `variants[]` 内的值为准。

## 环境与安装

需要 Python 3.10+。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

开发测试额外安装 pytest：

```bash
python -m pip install -e '.[dev]'
pytest -q
```

## 开发与供应商同步安全验收

默认分支是 `main`。开始开发前先读取仓库根目录的 `AGENTS.md`，并从根目录运行统一离线验收：

```bash
python scripts/verify_local.py
```

该入口会执行 Git whitespace check、tracked Python syntax、`pytest -q`、`config/*.json` 解析、Shijiu browser-exact Node helper syntax，以及 tracked `state/**` / `deliverables/**` 验收前后不变检查。

修改 `scraper.py`、`catalog.py` 或 Storefront 合约时，仍须按任务执行真实官网 read-only smoke；不能从离线 pytest 推断在线成功。Shijiu planning/dry-run 与 live write 必须分层，默认验收绝不执行 CREATE、UPDATE、图片上传、下架或恢复等生产写入。任何 Shijiu live write 都必须获得任务级明确授权，并具备显式 write gate、写后 readback 和脱敏证据。

Issue 驱动开发使用 `$issue-to-verified-push` 完成 feature branch、验证、push、remote SHA、PR evidence 和 final-CI gating；该 Skill 不自动 merge。

## 输入格式

默认读取仓库根目录的 `special_skus.csv`。推荐表头为 `product_number`；也兼容 `sku`、`品番`、`商品番号`，以及无表头时的第一列。

```csv
product_number
10-1105-495
```

品番必须符合 `NN-NNNN-NNN` 格式，重复项会保持原顺序去重。

正式 2026AW 批次使用仓库根目录的 `special_skus_2026aw.csv`。该清单按来源图片的页、行、列顺序保存，并包含以下人工复核字段：

```csv
product_number,gold_label,source_page,source_row,source_column,source_image
10-1105-495,false,1,1,1,bcb0b00b88fede9e366db73feb3bd488.jpg
```

图片中的 `GL` 仅记录为 `gold_label=true`，绝不会拼入 `product_number`。生产脚本会再次校验字段、品番格式、来源位置、原始顺序和重复项，任一清单结构错误都会在访问官网前停止。

## 运行

```bash
mikihouse-luyao --input special_skus.csv
```

等价的模块命令：

```bash
python -m mikihouse_luyao --input special_skus.csv
```

默认在官网商品请求之间等待 `0.5` 秒，以适应大批量任务的 API 限流。可按需要调整：

```bash
python -m mikihouse_luyao --input special_skus.csv --delay 0.8
```

默认输出：

- `output/products.json`
- `output/pdf/mikihouse_wechat_catalog.pdf`
- `output/failed_skus.json`
- `output/image-cache/`（按图片 URL 指纹缓存的官网原图）

可通过 `--json`、`--pdf`、`--failures` 和 `--image-cache` 修改路径。在线抓取优先使用官网公开的只读 Storefront API，商品 HTML 页面作为回退。Storefront 抓取会自动翻页读取全部 variant，并保存每个 variant 的 `selected_options`、SKU、`available_for_sale`、税入价、颜色、尺码、官网对应图片 URL 和原始像素尺寸。公开令牌轮换时可用 `MIKIHOUSE_STOREFRONT_TOKEN` 环境变量覆盖。

批量任务逐 SKU 隔离失败：单商品抓取或图片下载失败不会阻断其他商品。只要仍有成功商品，就会生成 JSON 和 PDF；失败项写入 `failed_skus.json`，进程返回码为 `2`，便于自动化任务识别“部分成功”。全部成功返回 `0`，全失败返回 `1`。

## 2026AW 正式批量生产

运行已经人工核对过来源位置的正式清单：

```bash
PYTHONPATH=src python scripts/production_2026aw.py
```

生产脚本会逐项真实访问 MIKI HOUSE 官网，并在完成后核对成功商品数与 PDF 页数（每页最多四件）。本地生产明细写入：

- `output/production-2026aw/products.json`：成功商品及逐变体官网税入价、最终人民币售价等底层数据
- `output/production-2026aw/failed_skus.json`：官网不存在或抓取异常的品番及原因
- `output/production-2026aw/review_required.csv`：图片内容不确定、需要人工判断的原始条目；本次已复核清单仅保留表头
- `output/production-2026aw/image-cache/`：官网下载的高清主图缓存

以上运行时明细和图片缓存均由 `.gitignore` 排除。可交付文件为 `deliverables/mikihouse_2026AW_price_catalog.pdf`，汇总数据为 `deliverables/production_report.json`。脚本只在存在成功商品、PDF 页数正确时更新这两个文件。

当前清单共 351 个唯一品番，其中 231 个带 `gold_label` 标记。最近一次完整在线生产的精确成功数、失败数、待复核数和 PDF 页数以 `deliverables/production_report.json` 为准。

最终交付前应把 PDF 全部页面以至少 200dpi 渲染并逐页人工检查。检查完成后运行以下命令，将页数、渲染尺寸、失败品番处置、文件大小、SHA-256 和 `visual_qa_passed` 写回正式报告：

```bash
PYTHONPATH=src python scripts/final_visual_qa.py \
  --use-existing-renders \
  --manual-review-passed
```

若尚未生成渲染图，省略 `--use-existing-renders`，脚本会调用 `pdftoppm` 在 `tmp/pdfs/final-200dpi-pages/` 生成全部页面。`--manual-review-passed` 只能在人工查看完每一页后使用；自动校验还会确认 311 个成功品番各出现一次、失败品番未进入 PDF、客户版没有内部定价文本，并拒绝空白页或低于 200dpi 的渲染。

本轮最终验收不再重试 40 个失败品番：它们按官网当前不可售或已下架处理，完整保留在 `output/production-2026aw/failed_skus.json`，并明确排除在正式 PDF 之外。

## 鞋类专用输出

鞋类依据官网标签和商品名识别，并在 PDF 中稳定排在其他商品之前。每个颜色严格使用该颜色 variant 的官网图片；所有图片按原始 URL 下载并缓存，PDF 只改变显示尺寸，不生成低分辨率缩略图。官网旧款若只提供 700×700 原图，程序保留其真实尺寸，不做虚假放大。

- 1 色：单张大图
- 2 色：左右双图
- 3-4 色：2×2 拼图
- 5-6 色：3×2 拼图
- 超过 6 色：自动使用半页宽卡片，完整显示所有颜色

每张图片下方显示对应颜色；拼图下方按颜色列出当前 `available_for_sale` 的尺码。只有排序后每一档都严格相差 0.5cm 时才会压缩为范围，存在断码时逐个列出。全商品同价只显示一次人民币售价；价格随颜色或尺码变化时，在对应尺码行显示人民币价格，并保留底部价格区间。

真实鞋类 smoke test 使用 12 款多颜色、多尺码商品，包含 3 款 7 色商品：

```bash
PYTHONPATH=src python scripts/shoe_smoke_test.py \
  --input shoe_smoke_skus_2026aw.csv \
  --output output/shoe-smoke-2026aw
```

报告 `output/shoe-smoke-2026aw/shoe_smoke_report.json` 会逐商品记录官网颜色图片、像素尺寸、当前可售尺码、variant SKU、税入价和人民币售价，便于与官网逐项复核。

## 可复现的端到端样例

仓库保存了从官方页面 JSON-LD 精简得到的 `10-1105-495` 测试夹具，可用于解析器的离线验证。完整 PDF 测试使用本地测试图片，不依赖网络：

```bash
pytest -q
```

## 真实官网 smoke test

`smoke_skus_2026aw.csv` 包含 12 个不同品类的真实商品，包括服装、鞋、包、袜、餐具和玩具，以及一个不同变体存在两档价格的回归商品。运行命令会真实抓取官网数据与原图，验证逐变体价格、输入顺序、失败报告、图片缓存、PDF 页数和内部定价信息隔离，并输出 `smoke_report.json`：

```bash
python scripts/smoke_test.py \
  --input smoke_skus_2026aw.csv \
  --output output/smoke-2026aw
```

## 当前范围

- 以官网 Shopify Storefront API 为主要数据源，抓取实时可售状态；`ProductGroup` JSON-LD 作为回退解析器。
- 校验请求品番与官网商品 handle 一致、必填字段完整，并拒绝非整数 JPY 价格或未完整读取的超大变体集合。
- 每个变体独立保存并计算价格；不同颜色或尺码价格不同时，PDF 会在对应颜色/尺码行标出人民币价格，并在卡片底部显示人民币价格区间。
- PDF 为 A4 2×2 商品卡片，适合直接通过微信发送；只显示商品图、商品名、品番、在售颜色/尺码和最终人民币售价。
- 商品名支持最多三行自适应换行；颜色/尺码按相同尺码集合与价格合并、压缩重复单位，并动态缩小字号以防止溢出。
- 税入日元底价、折扣、汇率和定价公式只存在于 JSON/底层计算，PDF 中不会出现。
- 普通商品主图及鞋类全部颜色图先下载并校验为有效图片，再从本地缓存嵌入 PDF，避免生成时出现远程图片空白。
- 页面结构变化时会快速失败，避免静默产生错误价格表。

## 第二阶段：全站商品主库

全站主库是与 2026AW PDF 完全独立的采集链路。它通过 Storefront API 的 `products` 游标分页读取当前全部可获取商品，并对超过 100 个 variant 的商品继续翻页。运行命令：

```bash
PYTHONPATH=src python scripts/sync_storefront_catalog.py
# 安装项目后也可运行：mikihouse-storefront-sync
```

默认输出到 `output/storefront-master/`：

- `master_catalog.json`：长期保存的商品主库，包含商品描述、完整 `images`/`media`/详情图、有序去重图片集合、颜色图片和全部 variants；
- `products.csv`、`variants.csv`：标准化商品表和 variant 表（UTF-8 BOM）；
- `incremental_changes.json`、`incremental_changes.csv`：本次新增、价格、库存、图片、元数据、下架及恢复变化；
- `crawl_stats.json`：全站分页、排除、商品/variant、图片和库存统计；
- `validation_report.json`：鞋类、服装、婴儿用品、杂货的真实样例校验。

`special_skus_2026aw.csv` 的 351 个品番是 PDF 专用池，也是 Shijiu 全流程的永久排除集合。它们在采集及 Shijiu CREATE/UPDATE/库存/图片/价格/下架恢复计划之前以 `excluded_reason=PDF_SPECIAL_LIST` 前置过滤；即使当前离线的 40 项以后恢复，仍不得进入小程序。普通商品按每个 variant 独立计算：

```text
mini_program_price_jpy = ceil(官网税入日元价 × 0.65)
```

该字段仍是日元整数，本模块不保存人民币售价、汇率或任何人民币换算结果。商品以 `product_number`、variant 以 `product_number::variant SKU` 为稳定标识。只有全站分页完整成功并通过跨品类、特殊品番和价格校验后才会原子更新主库；官网不再返回的商品或 variant 保留历史记录并标记 `active=false`，以后重新出现时记录为恢复上架。

统计会分别记录排除集合总数、本次官网实际遇到并排除的数量，以及当前官网未出现的特殊品番数量。2026-09-04 建立的业务基线为官网 2914 件、在线特殊品番 311 件、离线但永久记忆 40 件、非特殊候选 2603 件。本轮最终在线复核时官网已变为 2926 件，新增 12 件均为非特殊普通商品，因此实时池为 2615 件；具体新增品番记录在 `special_exclusion_report.json` 的 `live_observation`。官网随后出现新品时会作为独立 `NEW_PRODUCT` 记录；候选池仍严格等于“实时官网全站 − 351 永久排除”，报告同时保留基线与实时漂移，绝不会将新增普通商品误判为 legacy 或特殊商品。

Storefront 现在同时分页抓取 `images` 和 `media`，并解析描述中的详情图。每个商品生成 `ordered_images`，按“官网主图 → 其他商品/角度图 → variant 颜色图 → 详情图”排序并按源 URL 去重；`product_images_changed` 会覆盖这些集合的变化。

为便于 GitHub 审核，脚本同时把体积较小的抓取统计、增量变化摘要和分类验证报告写入 `deliverables/storefront_catalog/`；摘要包含完整变化文件的路径、大小、SHA-256 及代表样例。完整主库、逐条变化 JSON/CSV 和同步 CSV 属于运行数据，继续由 `.gitignore` 排除；本模块不会调用 PDF 生成器，也不会修改现有 2026AW PDF 成品。

### 稳定常规商品池（Shijiu 唯一允许的数据边界）

旧的“官网全站 − 351个PDF特殊品番”候选口径已经废弃。任何未来 Shijiu dry-run、增量事件或 live write 都必须使用 `output/storefront-stable/stable_catalog.json`，并在动作层再次运行相同过滤器；旧 `output/storefront-master/master_catalog.json` 只能作为历史候选池和差异基线，不能再直接生成 Shijiu 动作。

稳定池按以下互斥优先级分类：

1. `special_skus_2026aw.csv` 精确命中：`PDF_SPECIAL_LIST`；
2. title/name、tags、商品说明中明确出现 `WEB限定`、`WebLimited`、`WEB LIMITED`、`オンラインショップ限定/Online Exclusive` 等同义形式：`WEB_EXCLUSIVE`；
3. variant 的 `compareAtPrice > price`，或名称、标签、说明明确标注 `期間限定価格`、`特別価格`、`SALE/セール` 等促销价格：`LIMITED_TIME_PRICE`；
4. 只有 `webitem/WEBアイテム`、`期間限定` 等不足以证明前述规则的信号，或 compare-at 结构异常：`REVIEW_REQUIRED_STABILITY`；
5. 其余才进入 `STABLE`。

预约、受注等其它潜在不稳定标签只单独统计，不在没有新业务授权时扩大为永久排除。任何排除或复核商品即使在线，也不能产生 `NEW_PRODUCT`、`NEW_VARIANT`、`PRICE_CHANGED`、`INVENTORY_CHANGED`、`IMAGE_CHANGED`、下架或恢复动作。

只读全站刷新命令：

```bash
PYTHONPATH=src .venv/bin/python scripts/sync_stable_catalog.py
```

脚本完整分页读取 products、variants、images 和 media，并读取 variant `price/compareAtPrice`。只有全站分页成功才原子生成：

- `output/storefront-stable/source_catalog.json`：完整官网只读快照；
- `output/storefront-stable/stable_catalog.json`：Shijiu 唯一允许的稳定商品主库；
- `stable_products.csv`、`stable_variants.csv`、`excluded_products.csv`、`review_required_stability.csv`；
- `stable_incremental_changes.json`：只在稳定池内部产生的增量事件；
- `deliverables/storefront_stable_catalog/stable_catalog.json.gz`：可由 Git 跟踪的完整压缩稳定主库；
- `stable_pool_audit.json` 及排除/复核 CSV：计数、品番清单、旧池差异和显式信号零泄漏检查。

主库 schema 从2升级为3，只新增 nullable `compare_at_price_jpy`。读取旧schema 2时缺失值按 `null` 兼容；第一次用新抓取结果合并会明确记录 compare-at 字段变化，不会改变 `product_number`、`product_number::variant SKU`、历史 active/inactive 或65折JPY语义。

每个稳定商品保留全部 variant 的税入JPY、65折JPY、颜色、尺码、库存、variant图，完整有序主图/gallery/详情资源，符合 Shijiu richtext contract 的轻量 `shijiu_good_details`，抓取时间和规范化内容 SHA-256。图片本轮不下载到 Git、不上传 COS；资源清单保存官方 source URL、URL SHA-256 和有序描述符 SHA-256，明确不把它们冒充图片文件内容 hash。

2026-09-05 的真实只读全站结果为：官网2961件；在线PDF特殊311件、离线永久记忆40件；另排除WEB限定186件、明确促销价格2件；`46-8299-611` 因官网详情仍含一个非HTTPS旧图片资源进入复核。最终稳定池2461件/13782 variants/30172个有序去重图片资源。相对旧2615件候选池剔除188件、加入34件。全部商品名及其它可靠字段中的WEB限定/促销信号均为稳定池零泄漏，稳定池图片资源也实现零非HTTPS；完整证据见 `deliverables/storefront_stable_catalog/stable_pool_audit.json`。

### 稳定池自动增量规划

`scripts/sync_mikihouse_cycle.py` 是未来定时与人工“立即同步”的唯一共享入口。它保存 `state/mikihouse_source_sync_state.json.gz`，将官网真正下架/恢复与 WEB限定、限时促销、稳定性待复核隔离/恢复分别建模，并以规范化事件 ID + ledger 保证重跑幂等。详细转换和动作语义见 `docs/mikihouse_incremental_sync.md`。

离线运行（不重抓官网）：

```bash
PYTHONPATH=src .venv/bin/python scripts/sync_mikihouse_cycle.py --trigger manual
```

真实定时运行与手动运行共用同一核心，只需增加 `--refresh-storefront` 先完成官网全量只读 crawl。无论 trigger 为何，当前实现都在 `PLANNING_ONLY` 硬停止，没有 action execution 开关。输出为 `normalized_events.json`、`shijiu_action_plan.json` 和 `sync_cycle_report.json`，所有 action 均标记 `execution_allowed=false`。

本轮用上述2961件完整快照建立基线，包含18533个variants。对同一快照连续重放后是0个新事件、0个动作，`idempotent_replay_produced_no_new_events=true`。Shijiu/COS/上下架/价格库存写入均为0，未生成writer mutex evidence，未触碰legacy286。脱敏结果见 `deliverables/storefront_stable_catalog/sync_cycle_planning_report.json`、`future_automatic_sync_readiness.json` 和 `offline_incremental_sync_rehearsal.json`。

## Shijiu importer discovery 与 dry-run

本项目的最终目标端是 **Shijiu（世九）小程序后台**。`qinxitong8666/wawu-product-sync` 仅作为其中可明确定位到 Shijiu 的下游 client、字段样例、回读、checkpoint/resume、回滚和批处理安全机制的参考；瓦屋上游 API、瓦屋 mapper、瓦屋价格/分类/SKU 语义均未复用。证据边界和当前 main 缺失的真实成功写入证据详见 `docs/shijiu_downstream_contract_audit.md`。

当前 adapter 刻意不包含任何写方法，也没有写入 CLI 参数。Shijiu 客户端只允许商品/详情/分类 discovery 所需的语义只读端点：

- `/shopapi/Goods/index`：只列出固定类目的 legacy 参考商品；
- `/shopapi/goods/getFormatInfo`：只读取少量 legacy 样本的展示字段结构；
- `/shopapi/Goodtype/typeindex`、`/shopapi/Goodtype/index`、`/shopapi/goodtype/fatherIndex`：核对分类树。

首次 dry-run 命令：

```bash
PYTHONPATH=src python scripts/plan_shijiu_import.py \
  --target-env-file /absolute/path/to/shijiu.env
```

断点恢复使用同一主库和 checkpoint：

```bash
PYTHONPATH=src python scripts/plan_shijiu_import.py \
  --target-env-file /absolute/path/to/shijiu.env \
  --resume
```

MIKI HOUSE 是与 WAWU 平级且隔离的独立 provider，source 固定为 `MIKIHOUSE`。两者不得共享商品身份、variant 身份、同步状态或目标类目。适配器使用以下稳定标识，防止重复创建：

- `source_product_id = MIKIHOUSE:<product_number>`；
- `source_variant_id = MIKIHOUSE:<product_number>:<variant SKU>`；
- 目标端 `sku_code = MIKI-<variant SKU>`。

持久映射表为 `state/shijiu_mappings.json`，为每个新 product number 和 variant SKU 建立独立行。目标 variant 的稳定身份固定为 `shijiu_product_id + 精确 backend_sku_code`；官方 `getFormatInfo` 当前不提供独立 `sku_id`，因此 `shijiu_sku_id` 允许永久保持 `null`，不得猜测。目标商品 ID 只允许来自另行授权的新建成功回读，商品名匹配和对旧商品做 SKU reconciliation 均被明确禁止。

只读分类树确认 Shijiu 已有子类目 `MikiHouse`（ID `294884`，父类目 `母婴用品` ID `288338`）。所有可发布 MIKIHOUSE 商品固定使用 `good_type=294884`，不得按官网品牌或分类散落到其他 Shijiu 类目。官网 `brand`、`productType`、`category`、`tags` 只保存在 source metadata；由于没有已验证的 Shijiu 品牌 discovery 契约，`brand_id` 和 `supplier` 保持空值。

每个 variant 的 `sku_price` 和会员价格直接复制现有 `mini_program_price_jpy`，并再次验证其等于 `ceil(官网税入日元价 × 0.65)`；币种保持 JPY，不做人民币或汇率换算。Storefront 只提供 `availableForSale` 而没有库存件数，因此目标 `sku_stock` 保守映射为可售 `1`、不可售 `0`，审计数据同时保留原始布尔状态和来源说明。

每次全站抓取继续由 catalog 模块以上一次 master catalog 为基线，按 variant SKU 输出独立的 `NEW_PRODUCT`、`NEW_VARIANT`、`PRICE_CHANGED`、`INVENTORY_CHANGED`、`IMAGE_CHANGED`、下架及恢复事件。只有 `PRICE_CHANGED` 能生成 `UPDATE_PRICE_BY_EXACT_VARIANT_SKU`，且不会重新创建商品。价格保护配置位于 `config/shijiu_price_guard.json`：新价格越界，或绝对/相对变化超过阈值时只写入 `review_required.json`，不得进入自动更新计划。

Shijiu `MikiHouse` 类目中既有的 286 件商品统一定义为 `legacy_reference_only`，与新的 MIKIHOUSE source identity、SKU、价格和同步状态没有任何关系。在线 discovery 仅分页读取 286 条列表并均匀抽取 6 条详情，记录 `good_name`、`master_graph`、轮播/详情字段、SKU 规格和排序字段的结构，不保存样本业务内容，也不做匹配。`legacy_cleanup_plan.json` 为 286 件旧商品生成独立 `OFF_SHELF` 草案，但只有新商品完成另行授权的创建和回读验证后才能执行；本轮执行数始终为零。

完整字段预览、增量操作、checkpoint 和 Shijiu 只读快照写入 `output/shijiu-import/`；可追踪的精简动作、增量摘要、review required、跨鞋类/服装/婴儿用品/杂货各 5 件的 20 个完整 payload、legacy 结构审计、cleanup 草案、351 排除清单、价格校验、映射表和契约审计写入 `deliverables/shijiu_import/`。

dry-run payload 本身仍不可执行：官网图片 URL 只保留在 `source_content` 和 `image_upload_plan` 中，`master_graph`、`broadcast`、`good_detail_pics`、`sku_thumbnail` 是 `SHIJIU_COS_URL` 占位引用。详情 HTML 由当前 MIKI HOUSE 商品名、品番、品牌、描述、颜色、尺码和详情图片引用生成，绝不复制 legacy 商品内容。缺少官网图片的 7 件商品继续 `SKIP`，不使用其他商品图片替代。

## Shijiu 首批真实导入验证

`scripts/import_shijiu_first_batch.py` 是与只读 planner 分离的、仅用于冻结首批 20 件商品的 fail-closed 写执行器。批次固定在 `config/shijiu_first_live_batch.json`，包含鞋类、服装、婴儿用品和杂货各 5 件，共 176 个 variant、533 张有序官方图片。执行器具有以下硬门禁：

- 在任何目标请求前重新校验 351 个 `PDF_SPECIAL_LIST` 排除项，命中即失败；
- 固定 `source=MIKIHOUSE`、`good_type=294884`，不读取、绑定、更新或下架 legacy 286；
- 每件商品先逐图上传 `/v1/cos/upload`，所有占位引用替换为 Shijiu/COS HTTPS URL 后才创建；
- 新 canonical writer 固定采用 browser-exact 已验证的 `state=1`、`is_shelf=0`；`is_shelf=0` 是下架/不可见控制，不再使用失败首轮的 `state=0`；
- 每个上传和创建请求前写 checkpoint；已完成上传可以恢复，结果不明的上传或创建禁止自动重试；
- 创建后必须回读并验证商品 ID、精确后台 SKU 编码、类目、价格、库存、规格、主图、轮播和详情；验证通过后才原子更新 `state/shijiu_mappings.json`，独立 SKU ID 不存在时保持 `null`；
- 首个传输、字段或回读偏差立即停止整个批次，后续商品不会写入。

真实运行要求显式提供独立的目标端凭据文件和固定确认短语：

```bash
PYTHONPATH=src python scripts/import_shijiu_first_batch.py \
  --target-env-file /absolute/path/to/shijiu.env \
  --confirm MIKIHOUSE_FIRST_20_REAL_IMPORT
```

2026-09-04 的首次执行已按 fail-closed 规则停止：首件 `00-1000-028` 的 12 张官网图成功上传到 Shijiu/COS，但创建接口返回 `code=200, msg=success, data=[]`，既未返回商品 ID，延迟后覆盖上下架过滤的精确 `MIKI-00-1000-02800899999` 查询也仍为 0 条。因而不能证明商品实际创建，未做任何 ID 猜测或 mapping 绑定，后续 19 件的上传和创建均未执行，legacy cleanup 也未执行。由于没有可确认的商品 ID，也没有执行商品回滚；12 张 COS 图片作为已完成断点保留。冻结 checkpoint 禁止自动二次创建；再次运行会直接拒绝，必须先由人工确认目标接口为何返回空 ID，并另行授权如何处置。

随后按单件恢复授权完成了 `state` 语义核对和受控恢复。`config/shijiu_native_create_contract.json` 固定了参考仓库已审计原生 payload 的字段顺序与 `state=1/is_shelf=0`；执行前通过完整类目分页、34 组 SKU/名称精确查询和 mapping/checkpoint 检查证明首件无残留。恢复仅复用已有 12 张 COS 图片，图片上传请求为 0；只对 `00-1000-028` 发送 1 次创建，后续 19 件和 legacy 286 均为 0 次处理。

受控恢复响应仍是 `code=200, msg=success, data=[]`。创建后的多轮延迟查询和最终只读取证均未发现精确 SKU 或商品名，MikiHouse 类目仍为同一组 286 个 ID；因为没有候选 `product_id`，无法调用 `getFormatInfo` 核验 SKU ID、价格、规格、图片和详情。该商品因此仍不能认定已创建，mapping 保持空值，恢复 checkpoint 已终止且禁止第二次恢复创建。这个结果证明 `state=0` 与原生 `state=1` 的差异并非当前空响应/不可回读问题的充分解释，具体服务端拒绝原因仍缺少证据，不作猜测。

单件恢复命令（只能对新建或尚未消耗写预算的 checkpoint 使用）与纯只读取证命令：

```bash
PYTHONPATH=src python scripts/recover_shijiu_first_product.py \
  --target-env-file /absolute/path/to/shijiu.env \
  --confirm MIKIHOUSE_00_1000_028_RECOVERY_CREATE_ONCE

PYTHONPATH=src python scripts/recover_shijiu_first_product.py \
  --target-env-file /absolute/path/to/shijiu.env \
  --post-recovery-forensics
```

真实写入事实见：

- `deliverables/shijiu_import/first_live_batch_report.json`：写请求数量、图片上传、停止原因和逐商品状态；
- `deliverables/shijiu_import/first_live_batch_readbacks.json`：已完成的强校验回读（本次为 0）；
- `deliverables/shijiu_import/first_live_batch_forensics.json`：停止后的只读取证；
- `state/shijiu_first_live_batch_checkpoint.json`：逐图片/逐商品断点与创建响应；
- `deliverables/shijiu_import/first_product_residual_scan.json`：恢复创建前的完整只读无残留证明；
- `deliverables/shijiu_import/first_product_recovery_report.json`：单件恢复写预算、原生字段语义、响应和停止状态；
- `deliverables/shijiu_import/first_product_recovery_forensics.json`：恢复创建后的商品列表/SKU/名称多路径只读取证；
- `deliverables/shijiu_import/first_product_recovery_readback.json`：未取得唯一商品 ID、无法完成详情回读的明确失败记录；
- `state/shijiu_first_product_recovery_checkpoint.json`：一次性恢复 checkpoint，写预算已耗尽且为终止状态；
- `deliverables/shijiu_import/preflight_attempt_001_report.json`：首次使用错误分类 discovery 路径时的零写入预检记录，随后已改为仓库原先验证成功的 `Goodtype/typeindex`。

## Shijiu 最小创建诊断

`scripts/probe_shijiu_minimal_create.py` 是一次性、单候选、fail-closed 的契约诊断器。它永久拒绝 `00-1000-028`，从非 `PDF_SPECIAL_LIST` 商品池中按“当前可售、价格有效、未绑定、单 variant、图片最少、品番”稳定选择候选。新 checkpoint 只允许 1 次官方图片上传和 1 次新商品创建；只有同时从 `Goods/index` 与 `getFormatInfo` 取得唯一 product/SKU ID 并完成强校验，才允许对同一商品执行规格、轮播、详情三组渐进式 edit。终止 checkpoint 不能重试。

2026-09-04 实际选择 `17-1366-244`：官网实时验证为 1 个可售 variant、税入价 1650 JPY、65 折向上取整价 1073 JPY、1 张官方图片，且不在 351 个特殊品番中。图片上传成功并取得 `cdn0.19mini.com` URL；随后唯一创建请求采用固定类目 `294884`、`state="1"`、`is_shelf=0`、1 个真实 MIKI SKU 和 1 张 COS 图片。

本次还严格对齐了已审计 native fallback 的传输形态：移除旧 importer 的自定义 User-Agent 与 `Origin`，加入 `sec-ch-ua` 系列请求头，使用 `application/json;charset=UTF-8`，UTF-8 紧凑 JSON（`ensure_ascii=false`、分隔符 `,`/`:`），`secret`/`token` 位于 body 最前且 token 同时位于 query。服务端仍返回 HTTP 200、`code=200, msg=success, data=[]`，但创建后精确 SKU/名称查询均为空，MikiHouse 类目在所有完整过滤视图中仍是同一组 286 个 legacy ID，因此无法取得 product ID，也不能安全调用该商品的 `getFormatInfo`。结论是创建仍未被证明，不能把空 `success` 当作成功。

执行器按约定立即停止：写入总数为 2（1 次图片上传、1 次创建），规格/图片/详情 edit 为 0，mapping 绑定为 0，批量商品处理为 0，legacy 修改为 0，`00-1000-028` 创建为 0。由于最小创建阶段就失败，不能将问题归因到后续完整规格、轮播或详情字段组；现有证据也不足以确定具体是服务端校验、权限、租户还是工作流条件，项目不作猜测、不再换商品试写。

本轮证据见：

- `deliverables/shijiu_import/minimal_create_probe_candidate.json`：确定性候选选择与官网值；
- `deliverables/shijiu_import/minimal_create_payload_diff.json`：native 样例、旧完整 MIKI payload 与最小 payload 的逐字段名称、类型、值形态及传输差异；
- `deliverables/shijiu_import/minimal_create_probe_report.json`：唯一写入窗口、响应、319 次只读核验和停止原因；
- `deliverables/shijiu_import/minimal_create_probe_readback.json`：未取得唯一商品/SKU ID 的回读失败；
- `state/shijiu_minimal_create_probe_checkpoint.json`：已耗尽的一次性终止 checkpoint。

命令保留用于代码审计和全新、另行授权的 checkpoint；当前仓库 checkpoint 已终止，重新运行会在任何请求前拒绝：

```bash
PYTHONPATH=src python scripts/probe_shijiu_minimal_create.py \
  --target-env-file /absolute/path/to/shijiu.env \
  --confirm MIKIHOUSE_MINIMAL_CREATE_PROBE_ONE
```

## Shijiu 会话与 browser-exact 请求审计

`scripts/audit_shijiu_session.py` 只读取本地非 Git 配置、历史 native 模板和两仓库代码，不访问 Shijiu，也不包含任何商品写方法。2026-09-04 审计基线为本仓库 `becfc8b` 与 `qinxitong8666/wawu-product-sync@a36c5ea`。

WAWU 参考 writer 的真实行为是：`NATIVE_SAVE_REQUEST_PATH` 指向 Git 外的 `native_save_request.json`；发送前先加载模板 headers、明确移除模板中的 Cookie，再仅在本地配置存在 `MYSHOP_COOKIE` 时重新注入；body 仍为 `secret`、`token` 在前的 UTF-8 紧凑 JSON。当前本地私有配置只有 `MYSHOP_TOKEN/MYSHOP_SECRET`，没有 `MYSHOP_COOKIE/SHIJIU_COOKIE`，也没有覆盖 `NATIVE_SAVE_REQUEST_PATH`，因此上一轮 MIKIHOUSE create 确实没有 Cookie。

但 Cookie 缺失不是已证明的根因。历史 WAWU 直接调用闭环在同样不含 Cookie header 的情况下完成了创建、Goods.index/getFormatInfo 校验和测试商品清理。另一方面，历史浏览器 native 模板虽对应一次 `PASS_NATIVE_UI_SAVE_VISIBLE`，捕获器使用的是 Playwright `request.headers()`，没有使用 `request.allHeaders()` 或 CDP `requestWillBeSentExtraInfo`，所以模板中看不到 Cookie 并不能证明真实浏览器当时没有受保护请求头。

当前应用内浏览器没有 Shijiu 标签；本机 Chrome 虽在运行，但未安装/连接 ChatGPT 浏览器扩展，无法只读确认其是否有当前登录后台。现有 token/secret 已通过上一轮 319 次只读请求证明具有读取能力，但不能证明当前浏览器会话或商品创建权限。审计结论因此是 `BLOCKED_MISSING_BROWSER_EXACT_SESSION_EVIDENCE`：本轮 Shijiu read/upload/create/update 全部为 0，没有选择新候选，没有触碰 legacy，也不会继续更换 MIKIHOUSE 商品试写。

解除门禁前，需要人工在当前已登录的 Shijiu 后台私下取得以下最小证据，且全部保存在 Git 工作区之外：

1. 一次原生“新增商品→保存”的完整 Copy as cURL 或 HAR，或由 `request.allHeaders()`/CDP extra-info 捕获的请求；必须保留 method、完整 endpoint/query 形式、全部 request headers、Content-Type、原始 JSON body 和响应。如果真实请求含 Cookie，完整 Cookie 值只写入外部 `.secrets/shijiu.env` 的 `SHIJIU_COOKIE` 或 `MYSHOP_COOKIE`。
2. 与该请求对应的唯一测试商品 `Goods/index` product ID 和 `getFormatInfo` SKU ID，证明请求在当前会话下真实、持久落库；测试数据处置须另行明确。
3. 当前登录页能够访问 MikiHouse 类目 294884 的可见会话证明。若希望由 Codex 只读确认 Chrome 页面，需要先在 Chrome 安装并启用 ChatGPT 浏览器扩展。
4. 私有 native 模板路径通过外部配置 `NATIVE_SAVE_REQUEST_PATH` 指向；不得把 cURL、HAR、token、secret、Cookie 或原始请求体提交到仓库。

脱敏审计结果位于 `deliverables/shijiu_import/session_auth_audit.json`。报告只包含字段名、布尔状态、文件哈希和结构摘要；明确断言 token、secret、Cookie 与原始 body 值均未写入。

纯本地审计命令：

```bash
PYTHONPATH=src python scripts/audit_shijiu_session.py \
  --target-env-file /outside/git/shijiu.env \
  --native-template /outside/git/native_save_request.json \
  --native-capture-script /outside/git/capture.js \
  --native-result /outside/git/native_ui_result.json \
  --direct-loop-result /outside/git/direct_loop_result.json
```

## Shijiu browser-exact 本地捕获助手

`scripts/shijiu_browser_exact_capture.mjs` 用于解除上述 browser-exact 证据门禁。它同时监听 Playwright `request.allHeaders()` 与 CDP `Network.requestWillBeSentExtraInfo`，捕获人工在 Shijiu 原生后台执行一次保存时的完整请求和响应；保存后只读调用 `Goods/index` 与 `getFormatInfo`，要求唯一定位 `product_id`、核对商品与 SKU 结构，并如实记录目标响应是否暴露独立 `sku_id`。工具不会填写表单、不会点击保存，也不会自动创建测试商品。

该助手有两层强制安全边界：

- `--private-dir` 必须位于 Git 工作区之外，否则在启动浏览器或发送请求之前直接拒绝；完整 URL/query 值、headers、Cookie、token、secret、原始 body、响应和回读原文只写入该目录，权限为目录 `0700`、文件 `0600`；
- 捕获期间如发现 `good_type=294884`、`source=MIKIHOUSE` 语义或 `MIKI-` SKU，`newAddGood` 请求会在传输前被中止。人工样本必须是一个非 MIKIHOUSE、非 294884 类目的可删除测试商品；工具不会删除它，后续处置须由人工确认。

Git 中的 `deliverables/shijiu_import/browser_exact_capture_readiness.json` 只保存 endpoint/query/header 字段名、字段类型、公开 header 值的 SHA-256、私有证据文件 SHA-256、响应哈希和允许公开的 product/SKU ID，不保存任何认证值、原始 body 或响应值。报告会自动比较 browser-exact、历史 WAWU 成功请求和此前 MIKIHOUSE 请求的认证存在性、租户字段、headers、query、body 字段及类型，并只根据新证据给出修复结论。

安装本地依赖（`node_modules/` 已忽略）：

```bash
npm install
```

先运行零网络写入的预检：

```bash
npm run capture:shijiu -- \
  --mode preflight \
  --private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --historical-wawu-template /absolute/outside/repo/native_save_request.json
```

首次预检状态为 `BLOCKED_EXISTING_CHROME_NOT_ATTACHABLE`：Chrome 152 正在运行，但没有启用 ChatGPT Chrome 扩展，且当前进程没有开放本地 CDP 端口；不能在不重启的情况下附着现有默认 profile，也未读取其 Cookie、storage 或密码。已验证的最少人工路径是让脚本启动一个独立私有 profile：

```bash
npm run capture:shijiu -- \
  --mode capture \
  --launch-private-profile \
  --private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --historical-wawu-template /absolute/outside/repo/native_save_request.json \
  --confirm-capture SHIJIU_BROWSER_EXACT_HUMAN_SAVE_CAPTURE
```

浏览器打开后，人工登录 Shijiu，在原生后台只处理一个非 MIKIHOUSE 测试商品并点击一次保存，其余捕获、只读回读、脱敏比较和报告生成均自动完成。监听器现在作用于整个 browser context，覆盖登录后新开的后台标签页。不要复制或复用 Chrome 默认 profile。

后台登录入口包含私有登录参数时，应只在本地运行时通过
`SHIJIU_BROWSER_START_URL` 提供；工具会强制导航到该入口，即使持久 profile
恢复了同域旧标签页。该环境变量的值不会进入 Git 报告、README 示例或捕获摘要，
也不得写入仓库配置。

若已有专用的、非默认 Chrome profile，可先用 `--remote-debugging-port=9222 --user-data-dir=/absolute/private/profile` 启动它，再用下面的 CDP 模式；端口只应监听本机：

```bash
npm run capture:shijiu -- \
  --mode capture \
  --cdp-url http://127.0.0.1:9222 \
  --private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --historical-wawu-template /absolute/outside/repo/native_save_request.json \
  --confirm-capture SHIJIU_BROWSER_EXACT_HUMAN_SAVE_CAPTURE
```

若希望 Codex 直接检查现有 Chrome 标签，需要先在 Codex 设置的 Computer use 页面安装并启用 ChatGPT Chrome 扩展；这与上述独立 profile/CDP 捕获路径二选一即可。

2026-09-04 已完成一次真实 browser-exact CREATE 验证。工具通过运行时私有登录入口启动独立 profile，并在整个 browser context 捕获人工新增的非 MIKIHOUSE、非 294884 测试商品。原生请求返回 HTTP 200、`code=200/msg=success/data=[]`；随后 Goods.index 以精确商品名唯一取得 `product_id=9358232`，getFormatInfo 回读相同商品和 1 个 SKU 结构，证明该 CREATE 确实持久化。脱敏状态为 `BROWSER_EXACT_CAPTURE_VERIFIED`：

- 成功 CREATE 没有 Cookie/Authorization，query token 与 body token 均非空且相同；认证值只存在 Git 外私密证据；
- 成功 CREATE 与此前失败 MIKIHOUSE create 的 endpoint、query 名、Content-Type、顶层字段名/类型/顺序一致；当前独有 `Origin` header，浏览器公开 UA/client-hint 值不同，且此前 MIKI `sku_info` 多出非 canonical 的 `weight`；
- canonical writer 因此精确加入 `Origin` 和当前公开 browser header 形态、移除 `sku_info.weight`，并在发送前依据 `config/shijiu_native_create_contract.json` 强制校验字段顺序与类型；
- getFormatInfo 仍不暴露独立 SKU ID。稳定 variant 身份改为 `shijiu_product_id + 精确 backend_sku_code`，`shijiu_sku_id=null` 是正式契约，不再导致回读失败。

恢复回读支持 `--mode readback`（审计 client 直连）和 `--mode ui-readback`（复用私有后台页面的真实 Goods.index 身份，并对 CREATE 使用精确商品名搜索）；`--mode finalize` 只从 Git 外已有证据重建脱敏报告。最终证据见 `deliverables/shijiu_import/browser_exact_capture_readiness.json` 和 `browser_exact_capture_analysis.json`。

## Shijiu canonical 单商品真实验证

`scripts/validate_shijiu_canonical_create.py` 是后续批量前的一次性 fail-closed 验证器。它排除 351 个 `PDF_SPECIAL_LIST` 品番、`00-1000-028`、`17-1366-244` 和已有映射，只选择当前可售、单 variant、图片最少的新商品；发送前必须验证上述 browser-exact 私密证据哈希和 canonical payload。它允许上传所选商品的全部官方图片，但整个 checkpoint 最多只能发送 1 次商品 CREATE，终止后禁止重试。

本轮选中 `36-2001-572`：1 个 variant、1 张官网图片，官网税入价 2200 JPY，`mini_program_price_jpy=1430`，固定类目 294884、`state="1"/is_shelf=0`。图片上传成功，唯一 CREATE 返回与人工成功样本相同的空 `success` 响应。初版 runner 错把 Goods.index 的 `good_code` 当成 backend `sku_code` 主搜索入口，因此留下了可能假阴性的终止记录；该商品的 CREATE 预算已经耗尽，任何情况下均禁止再次创建。

后续 canonical CREATE 回读已修正为 browser-exact 证明过的主路径：Goods.index 按精确 `good_name` 定位 product_id，再逐个调用 getFormatInfo 强校验精确 backend `sku_code`、类目、JPY 价格、规格、主图、轮播和详情图片；`good_code` 搜索只保留为辅助证据，绝不能单独触发绑定。`shijiu_sku_id` 无官方明确字段时保持 `null`，稳定 variant 身份为 `shijiu_product_id + backend_sku_code`。

2026-09-04 已对历史唯一 CREATE 做一次严格只读 reconciliation。精确名称 `ヘアゴム（2個セット）` 在类目 294884 和全类目名称查询中均为 0；随后完整分页扫描 MikiHouse 类目，286 条记录在扫描前后计数一致且 product_id 全部唯一，仍无同名候选；`good_code` 辅助搜索也为 0。结论为 `RECONCILIATION_NO_UNIQUE_STRONG_EVIDENCE`：mapping 继续未绑定，`shijiu_product_id/shijiu_sku_id` 均为 `null`。本次只读核验发送 18 个 Goods.index 请求，CREATE、图片上传、更新和其他目标端 mutation 全部为 0；没有调用任何候选 getFormatInfo，因为没有候选 product_id。

随后使用 `scripts/shijiu_ui_context_reconcile.mjs` 完成了更严格的 UI-context reconciliation。工具从当前已登录的私有 Chrome profile 捕获商品列表页面真实 Goods.index 请求，完整复用实际 endpoint、headers、URL token、form secret、字段顺序及安全过滤上下文；全类目查询只改 `good_name`，MikiHouse 查询只额外设置必要的 `good_type=294884`。页面真实上下文包含 `recommend=2`、`push=2`，与前述独立客户端查询上下文不同。

UI-context 在类目 294884 和无类目限制两条路径都唯一找到 `product_id=9358233`。同一 BrowserContext 的 getFormatInfo 随后确认精确 `MIKI-36-2001-57200039999`、1430 JPY、类目 294884、规格 `紺,---` 及历史上传的主图/轮播/详情/SKU 图片全部一致，因此历史唯一 CREATE 已正式认定为成功持久化；mapping 已补写 `shijiu_product_id=9358233`，`shijiu_sku_id=null`。UI 回读没有暴露 `is_shelf`，仅在本 UI-context 校验中允许该字段缺失；若它明确返回非 0 仍会失败。36 个目标请求全部只读，新增 CREATE、上传、更新、legacy 操作和其他商品操作均为 0。

成功 browser CREATE 与 canonical MIKIHOUSE CREATE 的业务值脱敏比较也已记录：类目为 294880/294884；商品名为测试名/官网商品名；supplier 均为空；测试商品描述为空而 MIKI 带 source metadata 和详情模板；规格为 `g重=170g`/`颜色=紺、尺码=---`；SKU code 为空/精确 MIKI code；售价、成本、会员价和库存为 `100/100/100/140` 与 `1430/2200/1430/1`；两者主图和轮播均为 1 张 COS 图片但哈希不同。上述是观察到的业务值差异，不证明其中任一是拒绝原因——UI-context 已证明 MIKIHOUSE CREATE 实际成功。

只读 reconciliation 入口如下。它只接受 Git 外已验证的 private capture 目录，不需要也不接受写入确认词；代码会拒绝任何非 read 请求：

```bash
python scripts/reconcile_shijiu_canonical_create.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact
```

UI-context 流程分两步，原始请求、响应和认证值只写入 Git 外 private 目录；第二步仅离线验证及更新本地 state/report：

```bash
node scripts/shijiu_ui_context_reconcile.mjs \
  --private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact

python scripts/finalize_shijiu_ui_context_reconciliation.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact
```

证据文件：

- `deliverables/shijiu_import/canonical_create_candidate.json`：候选选择、官网价格和特殊名单边界；
- `deliverables/shijiu_import/canonical_create_validation_report.json`：历史唯一上传/CREATE、空响应及当前 reconciliation 汇总；
- `deliverables/shijiu_import/canonical_create_reconciliation_report.json`：精确名称、完整类目扫描、辅助 good_code 查询及零 mutation 的脱敏证据；
- `deliverables/shijiu_import/canonical_create_ui_context_reconciliation_report.json`：真实 UI 请求上下文、强回读、业务值差异及最终 mapping 证据；
- `state/shijiu_canonical_create_checkpoint.json`：已耗尽且终止的 checkpoint；
- `config/shijiu_native_create_contract.json`：当前 browser-exact canonical 字段、类型、顺序和 header 契约。

## Shijiu 复杂商品五件真实验证

`scripts/import_shijiu_complex_batch.py` 为复杂商品专用、逐商品 fail-closed 执行器。它从当前非特殊、未映射、可售主库中按五个确定性角色选择商品，永久排除 351 个 `PDF_SPECIAL_LIST` 品番以及此前测试过的 `00-1000-028`、`17-1366-244`、`36-2001-572`。冻结批次位于 `config/shijiu_complex_live_batch.json`，后续恢复不会因已完成 mapping 而重新选择商品。

本轮自动选择结果为：

- 多颜色多尺码鞋类 `13-9310-490`：24 variants、4 色、6 尺码、42 图；
- 高 SKU 服装 `10-1829-685`：18 variants、6 色、3 尺码、66 图；
- 多轮播/详情图商品 `10-8227-686`：6 variants、69 图；
- 婴童用品 `00-4000-054`：3 variants、31 图；
- 普通杂货 `10-8223-684`：3 variants、19 图。

5 件共 54 个 variants、227 张有序官方图片。写前已在线确认全部 SKU、税入 JPY 价格、库存、颜色、尺码和 variant 图片与当天 master catalog 一致；每个 `mini_program_price_jpy` 继续由 `ceil(tax_included_price_jpy×0.65)` 校验，不做人民币换算。官网说明中的普通链接会在详情模板生成前移除，正式 Shijiu 图片字段和详情中不得残留 MIKI HOUSE 外链。

执行器从 Git 外已验证 capture 同时加载 browser-exact CREATE contract 和真实 UI Goods.index 请求。UI 查询保持 endpoint、headers、form 字段顺序、URL token、body secret、`recommend=2`、`push=2` 等上下文，只改变精确 `good_name`、必要的 `good_type` 和分页；`good_code` 不参与主判定或 mapping。每件商品必须完成全部 COS 上传、唯一 CREATE、精确名称定位及 getFormatInfo 全 SKU/价格/库存/规格/图片强校验后才会写 mapping 并进入下一件。`shijiu_sku_id` 无官方字段时始终为 `null`。

```bash
# 只核对官网并冻结候选；不访问 Shijiu
PYTHONPATH=src python scripts/import_shijiu_complex_batch.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --prepare-only

# 真实写入只能在新的、明确授权批次使用精确确认词
PYTHONPATH=src python scripts/import_shijiu_complex_batch.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --confirm MIKIHOUSE_COMPLEX_5_REAL_IMPORT

# 冻结后的延迟 reconciliation 仅执行 Goods.index/getFormatInfo 读取
PYTHONPATH=src python scripts/import_shijiu_complex_batch.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --reconcile-only
```

2026-09-04 的实际执行在首件即按规则停止。`13-9310-490` 的 42 张官网图片全部取得 Shijiu/COS URL，唯一 CREATE 返回与已验证原生保存相同的 `code=200/msg=success/data=[]`；随后 UI-context 在类目 294884 和无类目限制下均未找到精确商品名候选。延迟只读 reconciliation 结果仍为 0 个候选，因此该 CREATE 不能认定持久化，mapping 保持未绑定，绝不重试。

后四件的图片上传和 CREATE 均为 0；没有对 legacy 286 做 identity reconciliation、绑定或修改，cleanup 为 0。目标端总计 42 次图片上传、1 次 CREATE、7 次只读请求、0 次 UPDATE。批次状态为 `STOPPED_ON_FIRST_ERROR`，checkpoint 已冻结。由于五件未全部通过，本轮没有生成、更没有执行下一阶段 20 件计划，readiness 明确为 `BLOCKED_AFTER_FIRST_COMPLEX_CREATE_ANOMALY`。不对复杂 CREATE 未持久化的具体字段原因作无证据推断。

脱敏证据：

- `deliverables/shijiu_import/complex_live_batch_candidates.json`：确定性选择、复杂度指标和官网在线核验；
- `deliverables/shijiu_import/complex_live_batch_report.json`：写入计数、逐商品结果与停止原因；
- `deliverables/shijiu_import/complex_live_batch_readbacks.json`：强回读结果，本轮为 0 件通过；
- `deliverables/shijiu_import/complex_live_batch_readiness.json`：冻结与下一阶段未就绪结论；
- `state/shijiu_complex_live_batch_checkpoint.json`：逐图片、逐 CREATE 和只读 reconciliation 断点。

## Shijiu CREATE 复杂度二分验证

原 5 件复杂商品批次永久保持冻结，`13-9310-490` 不重试，后四件也不恢复。`scripts/import_shijiu_complexity_bisection.py` 使用全新的候选配置、checkpoint、确认词和报告路径，硬性排除全部历史尝试品番、351 个 `PDF_SPECIAL_LIST` 品番、已有 mapping 以及 legacy 286。两个探针严格串行：第 1 件未完成 UI-context 精确名称定位、getFormatInfo 全字段强校验和 mapping 持久化时，第 2 件不允许上传或 CREATE。

离线重建实际 resolved payload 后，成功的 `36-2001-572` 与未持久化的 `13-9310-490` 对比如下：

| 指标 | `36-2001-572` 成功 | `13-9310-490` 未持久化 |
|---|---:|---:|
| business payload UTF-8 bytes | 2,324 | 26,597 |
| 含认证信封 wire body bytes | 2,411 | 26,684 |
| SKU / 规格维度 / 选项总数 | 1 / 2 / 2 | 24 / 2 / 10 |
| broadcast URL / 字符数 | 1 / 76 | 42 / 3,233 |
| good_details 字符 / UTF-8 bytes / 图片 | 235 / 489 / 0 | 5,821 / 9,331 / 38 |
| good_detail_pics URL / 字符数 | 0 / 0 | 38 / 2,925 |

报告同时列出每个字符串字段的最大字符数和 UTF-8 bytes，不保存 token、secret 或 Cookie。只读参考证据显示：已提交的 WAWU→Shijiu 唯一回读 CREATE 记录已成功到单商品 11 SKU；现存 legacy 只读样本中 getFormatInfo 可读取 24 SKU 商品。后者只证明存储/读取能力，不单独证明当前 canonical CREATE 可接受 24 SKU。

本轮自动冻结的二分候选为：

- 图片/详情探针 `00-4000-057`：4 variants、74 张轮播图、70 张详情图；
- SKU 探针 `63-6602-492`：14 variants、6 张轮播图、4 张详情图。

两件的官网 SKU、税入 JPY 价格、65 折 JPY 价格、库存、颜色、尺码和 variant 图片均在写前在线核对。第 1 件完成 74 次 COS 上传并只发送 1 次 canonical CREATE；CREATE 后即时及冻结后延迟 UI-context 查询在类目 294884 和全类目中都没有精确名称候选，不能认定持久化，mapping 未写入。执行器立即永久冻结该批次：第 2 件保持 `PLANNED`，图片上传和 CREATE 均为 0。

截至最终只读 reconciliation，目标请求总计 74 次图片上传、1 次 CREATE、61 次只读查询、0 次 UPDATE、0 次 legacy 操作。结论为 `IMAGE_OR_DETAIL_SCALE_SUSPECTED_SKU_PROBE_NOT_RUN`：4 SKU 已低于现有 11-SKU 成功 CREATE 证据，而实际探针 payload 为 28,045 wire bytes、74 轮播、70 详情图，仍未持久化，因此图片/详情规模是当前最强嫌疑；这只是受控证据指向，不等同于已证明服务器硬上限。由于首件失败，14-SKU 探针按规则永久不执行，不能据此宣布 SKU 规模已通过或失败，也不生成 20 件批量计划。

失败的 `13-9310-490` 原 42 张 COS 图片登记在 `deliverables/shijiu_import/orphan_cos_assets_13_9310_490.json`，保留原 upload reference、顺序、角色和目标 URL；本批次不删除、不重新上传，也不用于其他商品。

```bash
# 冻结候选、在线核对官网、生成离线规模报告；零 Shijiu 请求
PYTHONPATH=src python scripts/import_shijiu_complexity_bisection.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --wawu-evidence /absolute/read-only/wawu-multisku-evidence.json \
  --prepare-only

# 仅在新的明确授权下，最多两个、严格串行的真实探针
PYTHONPATH=src python scripts/import_shijiu_complexity_bisection.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --wawu-evidence /absolute/read-only/wawu-multisku-evidence.json \
  --confirm MIKIHOUSE_COMPLEXITY_BISECTION_2_REAL_IMPORT

# 冻结后只读 reconciliation；绝不恢复第二件
PYTHONPATH=src python scripts/import_shijiu_complexity_bisection.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --wawu-evidence /absolute/read-only/wawu-multisku-evidence.json \
  --reconcile-only

# 从既有 checkpoint 重建脱敏结论；零网络请求
PYTHONPATH=src python scripts/import_shijiu_complexity_bisection.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --wawu-evidence /absolute/read-only/wawu-multisku-evidence.json \
  --finalize-reports-only
```

本阶段证据：

- `deliverables/shijiu_import/create_payload_scale_comparison.json`：两次历史 CREATE 的完整离线复杂度量化及多 SKU 只读证据；
- `deliverables/shijiu_import/complexity_bisection_candidates.json`：两个冻结候选与官网在线核验；
- `deliverables/shijiu_import/complexity_bisection_report.json`：请求计数、逐商品状态和 fail-closed 原因；
- `deliverables/shijiu_import/complexity_bisection_readbacks.json`：强回读结果；
- `deliverables/shijiu_import/complexity_bisection_diagnosis.json`：实际探针 payload 指标和诊断边界；
- `state/shijiu_complexity_bisection_checkpoint.json`：独立逐图、CREATE 和 reconciliation 断点。

## Shijiu 14-SKU 独立探针与富媒体容量经验审计

原复杂 5 件批次和上述两件二分批次继续永久冻结：不重试 `13-9310-490` 或 `00-4000-057`，也不恢复旧 checkpoint 中的第二件。经新的单商品明确授权，`63-6602-492` 被复制为一个全新、独立的验证边界；新配置、确认词、checkpoint 和报告均与旧二分批次隔离。执行器在写前逐文件校验两个旧批次的冻结哈希，并要求先存在零写入的富媒体容量审计，不能借此解冻旧批次。

容量审计复用 browser-exact UI 上下文，只允许 `Goods.index` 和 `getFormatInfo`。当前列表上下文声明 3,840 件商品；为避免以数千次详情请求冲击生产后台，审计固定读取首尾及中间等距分布的 32 页，并强制加入 browser-exact 非 MIKI 测试商品、6 件 legacy 只读样本和已映射成功样本，共读取 328 件唯一商品详情。360 次目标请求全部为只读，CREATE、UPDATE、图片上传和 legacy 修改均为 0。报告不保存商品名、商品 ID 原值、图片 URL 或认证值。

下表将三次历史 payload 与目标端抽样观察最大值放在一起。最后一列是“各字段分别取最大”的组合行，不保证来自同一商品，也不代表目标全库最大值或服务器硬限制：

| 指标 | `36-2001-572` 成功 | `13-9310-490` 未持久化 | `00-4000-057` 未持久化 | 目标端只读经验最大值 |
|---|---:|---:|---:|---:|
| SKU 数 | 1 | 24 | 4 | 60 |
| broadcast 字符 / URL | 76 / 1 | 3,233 / 42 | 5,697 / 74 | 991 / 16 |
| good_detail_pics 字符 / URL | 0 / 0 | 2,925 / 38 | 5,389 / 70 | 615 / 8 |
| good_details 字符 / UTF-8 bytes / 图片 | 235 / 489 / 0 | 5,821 / 9,331 / 38 | 9,625 / 13,442 / 70 | 1,024 / 2,560 / 0 |

`63-6602-492` 写前在线核验为 14 variants、2 色、7 尺码、6 张轮播、4 张详情图，planned business payload 为 7,940 UTF-8 bytes。实际执行上传 6 张官方图片并发送唯一 1 次 browser-exact canonical CREATE；UI-context 以精确名称取得唯一 `shijiu_product_id=9358241`，随后 `getFormatInfo` 对全部 14 个精确 backend SKU、8,580 JPY 的 65 折价格、库存、颜色/尺码规格、SKU 图片、主图、完整轮播、详情图和类目 294884 全部通过。mapping 已持久化，14 个 `shijiu_sku_id` 因官方回读无该字段而保持 `null`。本轮合计 6 次图片上传、1 次 CREATE、6 次只读请求、0 次 UPDATE、0 次 legacy 操作；没有选择替代品，也没有生成或执行 20 件批次。

结果将“当前 canonical CREATE 至少支持 14 SKU”标记为已验证。结合两个富媒体重商品均未持久化，剩余规模信号主要收敛到富媒体字段，但仍不能从现有样本推出服务器硬限制或单一根因。下一阶段草案采用：CREATE 仅提交核心商品、完整规格/SKU、主图及最多 4 张受控轮播；其余轮播、详情图片和最终详情 HTML 使用仓库已审计的 Shijiu 原生 edit 路径，按完整 payload 重提、分阶段补齐。每步写前保存完整 getFormatInfo 快照，写后强回读，任一不一致即冻结；真实 UPDATE 仍须新的明确授权。本轮只生成草案，未发送任何 UPDATE。

```bash
# 严格只读容量经验审计；不包含任何写接口
PYTHONPATH=src python scripts/audit_shijiu_rich_media_capacity.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact

# 在线核验官网并冻结独立候选；零 Shijiu 请求
PYTHONPATH=src python scripts/import_shijiu_high_sku_probe.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --prepare-only

# 已消费的一次性写入命令；完成或失败后的 checkpoint 都禁止再次 CREATE
PYTHONPATH=src python scripts/import_shijiu_high_sku_probe.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --confirm MIKIHOUSE_HIGH_SKU_14_SINGLE_REAL_IMPORT
```

本阶段证据：

- `deliverables/shijiu_import/rich_media_capacity_empirical_audit.json`：抽样覆盖、SKU 分布、三份历史 payload 与目标端经验最大值；
- `config/shijiu_high_sku_14_probe.json`：独立候选、永久排除集合、容量审计与旧冻结文件哈希；
- `deliverables/shijiu_import/high_sku_14_probe_candidate.json`：官网在线复核和 canonical 私密证据哈希；
- `deliverables/shijiu_import/high_sku_14_probe_report.json` 与 `high_sku_14_probe_readbacks.json`：写入计数和全部 14 SKU 强回读；
- `deliverables/shijiu_import/high_sku_14_probe_diagnosis.json`：14-SKU 已通过及剩余证据边界；
- `deliverables/shijiu_import/staged_rich_media_update_plan.json`：最初的分阶段富媒体方案及下一阶段实际验证状态；
- `state/shijiu_high_sku_14_probe_checkpoint.json` 与 `state/shijiu_mappings.json`：单商品幂等断点和稳定映射。

## Shijiu 单商品分阶段富媒体真实验证

本阶段只选择一个全新商品，历史失败/冻结批次保持不可恢复，`13-9310-490`、`00-4000-057` 没有重试，已验证的 `36-2001-572`、`63-6602-492` 没有修改。确定性规则从非 351 `PDF_SPECIAL_LIST`、未映射且可发布的源商品中选择名称唯一、2–6 variants、20–35 张有序官方图并同时包含商品图与详情图的候选；当前冻结候选是 `10-8375-578`（2 variants、27 张图）。官网在线核验确认两个 SKU 的价格、库存、颜色、尺码和 variant 图与 master 一致。

执行器 `scripts/import_shijiu_staged_rich_media.py` 每次启动最多推进一个商品保存步骤。CREATE 一次提交完整规格与全部 SKU，但只带主图、4 张有序轮播、空 `good_detail_pics` 和不含 URL 的最小详情文本；每次 UPDATE 前先持久化完整 `getFormatInfo` 快照，随后只使用带整数商品 ID、完整 canonical 字段、完整规格和完整 SKU 的原生 full-payload edit。轮播与详情图每步最多增加8张，每步均以 UI-context 精确商品名定位唯一商品，再用 `getFormatInfo` 校验全部 SKU、JPY 价格、库存、规格、主图及有序图片。不存在 PATCH 式更新或自动回滚。

真实结果：CREATE 使用5张实际必需的 COS 图片，成功创建下架商品 `shijiu_product_id=9358250`；两个 backend SKU 的售价均为 71,500 JPY，库存均为1，mapping 已持久化且 `shijiu_sku_id=null`。轮播 4→12 和 12→20 的两次 full-payload UPDATE 均完整回读通过，SKU、价格、库存和规格未变化。因此当前商品已观察到的稳定成功值为20张有序轮播（高于此前只读样本中的16张），但这不是服务器硬限制。

计划测试 20→27 时，第22号详情图来自官方商品数据使用的 `img.mksk.me` CDN；当时下载白名单尚未包含该域名，工具在下载、COS上传和商品 UPDATE 之前本地拒绝并立即冻结。目标端没有收到该阶段上传或 UPDATE；之后只读 UI-context 再次确认商品仍准确保持20张轮播、0张 `good_detail_pics` 和2个正确 SKU。代码现已把该官方详情 CDN 纳入“可下载但正式 payload 仍禁止热链”的安全策略，但冻结商品绝不重试。因此27张轮播、详情图片和最终详情 HTML 容量均仍未验证，不能视为目标端拒绝或服务器上限。

```bash
# 只读官网核验、冻结候选和阶段计划；不会调用 Shijiu 写接口
PYTHONPATH=src python scripts/import_shijiu_staged_rich_media.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --prepare-only

# 一次调用只推进一个 CREATE 或 full-payload UPDATE；checkpoint 终态禁止重试
PYTHONPATH=src python scripts/import_shijiu_staged_rich_media.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --next-step \
  --confirm MIKIHOUSE_STAGED_RICH_MEDIA_SINGLE_STEP

# 冻结后仅用 UI-context 对最后成功状态做强回读，不发送写请求
PYTHONPATH=src python scripts/import_shijiu_staged_rich_media.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --readonly-confirm-frozen
```

新增证据文件：

- `config/shijiu_staged_rich_media_single.json`：单商品选择、9个阶段及历史冻结文件哈希；
- `state/shijiu_staged_rich_media_single_checkpoint.json`：逐图、逐次保存、写前快照、强回读与冻结断点；
- `deliverables/shijiu_import/staged_rich_media_candidate.json`：官网在线核验及脱敏 browser-exact 证据；
- `deliverables/shijiu_import/staged_rich_media_validation_report.json` 与 `staged_rich_media_validation_readbacks.json`：逐阶段结果和完整强回读；
- `deliverables/shijiu_import/staged_rich_media_capacity_conclusion.json`：最后成功状态、未发送的首个阻断状态及经验容量边界。

## Shijiu 全资源预检与完整富媒体验证

本轮将上一件 `10-8375-578` 永久保持在已验证状态：`shijiu_product_id=9358250`、20张有序轮播和原 mapping 均未改动，旧 checkpoint 没有恢复。所有历史失败或冻结商品仍在候选前置排除集合中，351 个 `PDF_SPECIAL_LIST` 品番仍不可进入 Shijiu 任何阶段。

新的确定性候选是 `10-9129-792`（`キャップ（帽子）（大人用）`）：名称在当前 source 中唯一，3 variants，27 张有序官方图，同时含 gallery 和 detail。在任何 COS 或商品写入之前，工具先枚举 main/gallery/variant/detail 全部去重资源，先整体检查 HTTPS 与精确域名边界，再逐张完整下载并验证 MIME、图片解码、尺寸、字节数和内容 hash。实际 27/27 全部通过，预检期间 Shijiu 请求与写请求都是0。官方资源域支持精确域或子域匹配，包括 Storefront 已发现的 `img.mksk.me`；HTTP、伪后缀域、未知域、跨域重定向、过大文件或无法解码图片均在零目标写入状态下阻断。

真实运行的轻量 CREATE 使用完整规格与3个 SKU、4张轮播、空 `good_detail_pics` 和无图片 URL 的最小 HTML，成功创建不可见商品 `shijiu_product_id=9358255`。三个 backend SKU 均以 `ceil(16500×0.65)=10725 JPY`、库存1、对应颜色/尺码/图片和类目294884通过 UI-context 精确名称 + `getFormatInfo` 强回读；mapping 已落库，`shijiu_sku_id=null`。随后三次 native full-payload UPDATE 将轮播按序4→12→20→27全部补齐，每步 SKU、JPY价格、库存、规格和图片顺序均通过强回读。因此27张有序轮播是新的已观察稳定值，但仍不是服务器硬上限。

在首个详情图阶段，完整 `getFormatInfo` 写前快照已持久化，但后续只读 UI-context `Goods.index` 全类目扫描第9页返回 HTTP 502。此时该阶段 `attempts=0`、不存在 payload hash，请求账本也只有1次 CREATE 和前述3次轮播 UPDATE；所以可证明详情图 UPDATE 没有发送，目标状态仍是写前快照中的27张轮播、0张详情图和3个正确 SKU。按 fail-closed 规则，商品已永久冻结，没有重试、回滚或替换候选。该异常不是详情容量拒绝，详情图和最终 HTML 尚未验证，不能标记为生产架构已验证。因此本轮不生成、不执行下一批20件计划。

```bash
# 首次冻结候选；只在写前生成候选与在线 source 核验
PYTHONPATH=src python scripts/import_shijiu_staged_rich_media_complete.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --prepare-only

# 完整官方图片可读/MIME/解码预检；严格零 Shijiu 请求
PYTHONPATH=src python scripts/import_shijiu_staged_rich_media_complete.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --preflight-resources

# 每次最多消耗一个 CREATE/UPDATE；终态 checkpoint 永久拒绝继续
PYTHONPATH=src python scripts/import_shijiu_staged_rich_media_complete.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --next-step \
  --confirm MIKIHOUSE_STAGED_RICH_MEDIA_COMPLETE_SINGLE_STEP

# 只从已有 checkpoint 归一化脱敏证据；零网络、零 Shijiu 请求
PYTHONPATH=src python scripts/import_shijiu_staged_rich_media_complete.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --finalize-evidence-only
```

本阶段脱敏证据：

- `config/shijiu_staged_rich_media_complete_single.json`：确定性候选、全历史禁用集合和旧证据/mapping hash；
- `state/shijiu_staged_rich_media_complete_single_checkpoint.json`：27张全资源预检、逐图 COS、逐阶段快照、请求账本与冻结边界；
- `deliverables/shijiu_import/staged_rich_media_complete_candidate.json` 与 `staged_rich_media_complete_resource_preflight.json`：候选与完整资源可读证据；
- `deliverables/shijiu_import/staged_rich_media_complete_validation_report.json` 与 `staged_rich_media_complete_readbacks.json`：CREATE、三次 UPDATE 和强回读；
- `deliverables/shijiu_import/staged_rich_media_complete_capacity_conclusion.json`：27轮播已验证、详情阶段未发送及非硬上限结论；
- `deliverables/shijiu_import/staged_rich_media_complete_readiness.json`：生产架构未完整验证，下一20件计划未生成/未执行。

## Shijiu 详情图与最终 HTML 单商品验证

本阶段永久保留 `10-9129-792→9358255` 的27张已验证有序 broadcast 和 mapping，不恢复其冻结 checkpoint，也不对任何历史失败/冻结品番重试。新的独立模式使用 `config/shijiu_staged_detail_html_single.json` 和 `state/shijiu_staged_detail_html_single_checkpoint.json`，在选品时前置排除351个 `PDF_SPECIAL_LIST`、已映射商品与全部历史尝试品番。

UI-context 的 `Goods.index` / `getFormatInfo` 现在仅对 HTTP 502/503/504、连接超时和临时网络错误允许初次请求后最多3次重试，按0.5、1、2秒指数退避；每次尝试均在账本中标记为只读。HTTP 400、业务合约失配或响应结构异常不重试。CREATE、UPDATE 和 COS 上传始终零自动重试；写后读取失败时也只重试读取，不会重发 mutation。

确定性候选为 `10-5292-148`（`【WEB限定】ワッペンロゴ長袖Ｔシャツ（大人用）【WebLimited】`）：名称唯一、6 variants（2色×3尺码）、18张 broadcast、16张 detail pics，并同时含官方 gallery/detail 资源和带图片的最终 HTML。写前在零 Shijiu 请求状态下完成18/18张去重官方图的 HTTPS、域名、重定向、MIME、完整下载、解码与尺寸预检；官网6个 SKU 的6600 JPY、65折4290 JPY、库存、颜色、尺码和 variant 图也全部实时一致。

轻量 CREATE 成功建立不可见商品 `shijiu_product_id=9358309`，6个精确 backend SKU 均强回读通过并写入 mapping，`shijiu_sku_id=null`。随后broadcast 4→12→18的两次 full-payload UPDATE 均通过有序图片与所有SKU/价格/库存/规格不变校验。`good_detail_pics 0→8` 的唯一 UPDATE 也返回成功，随后脱敏快照确认目标端实际保存了8张有序详情图、18张broadcast、6个正确SKU及原价格/库存/规格。

但当次在线强回读被旧本地校验器误判：它要求分阶段 `good_detail_pics` 必须立即出现在当时仍按设计保持为最小文本的 `good_details` 中。根因修正后，已保存快照离线通过完整SKU、价格、库存、规格、主图、broadcast、detail pics、最小HTML和类目合约。但遵守首次异常即停规则，原 checkpoint 继续永久冻结，不重试 mutation、不继续16张详情图、不安装最终 HTML。因此生产架构仍为未完整验证，本轮不生成也不执行下一20件计划。实际请求账本为18次COS上传、1次CREATE、3次UPDATE，没有瞬时读错误或读重试，mutation重试为0。

```bash
# 冻结候选并实时核验官网；零 Shijiu 请求
PYTHONPATH=src python scripts/import_shijiu_staged_detail_html.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --prepare-only

# 全资源预检；严格零 Shijiu 请求/写入
PYTHONPATH=src python scripts/import_shijiu_staged_detail_html.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --preflight-resources

# 一次最多推进一个商品保存；终态checkpoint永久拒绝继续
PYTHONPATH=src python scripts/import_shijiu_staged_detail_html.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --next-step \
  --confirm MIKIHOUSE_STAGED_DETAIL_HTML_SINGLE_STEP

# 仅从既有快照重建脱敏取证/结论；零网络、零写入
PYTHONPATH=src python scripts/import_shijiu_staged_detail_html.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --finalize-evidence-only
```

脱敏证据位于 `deliverables/shijiu_import/staged_detail_html_*.json`，包括候选、全资源预检、逐阶段回读、容量结论、假失败根因和 readiness。

## Shijiu 生产架构终局单商品验证

本轮建立独立的 `MIKIHOUSE_PRODUCTION_ARCHITECTURE_FINAL_E2E_VALIDATION` 模式，不恢复或覆盖任何历史 checkpoint。候选选择前置排除351个 `PDF_SPECIAL_LIST`、全部历史配置中的尝试/冻结品番、所有已映射商品和源站重名商品；候选还必须有2–8个 variants、12–20张 broadcast、16–20张 detail pics，以及可替换为COS URL的图片型完整详情HTML。

确定性候选为 `63-3210-146`（`ツーウェイパンツ`）：名称唯一、7 variants、17张 broadcast、16张 detail pics。官网实时核验确认全部 SKU、税入价、65折 JPY 售价、库存、颜色、尺码和 variant 图与 master 一致。正式写入前完成17/17个有序去重资源的 HTTPS、官方域/子域、重定向、MIME、完整下载、图片解码、尺寸和内容 hash 预检，预检期间 Shijiu 请求与写入均为0。

轻量 CREATE 创建下架商品 `shijiu_product_id=9358329`，随后 `broadcast 4→12→17`、`good_detail_pics 0→8→16` 均通过 UI-context 精确名称定位和 getFormatInfo 强回读；7个 backend SKU、15730 JPY售价、逐SKU库存、颜色尺码、规格、主图、轮播、详情图和类目294884保持一致，mapping 已持久化且 `shijiu_sku_id=null`。

最终 `good_details` full-payload UPDATE 仅发送一次并返回 `code=200/msg=success`，但强回读显示目标端仍保留前一阶段的最小文本 HTML，没有保存预期的16张COS图片型HTML。冻结后只读快照同时证明17张broadcast、16张detail pics、全部SKU/价格/库存/规格/主图和类目没有回归。这不是旧校验器假阴性，而是目标端未持久化最终HTML。因此 checkpoint 按首次异常永久冻结，不重发 mutation、不自动回滚、不更换商品；`production_import_architecture_verified=false`，20件冻结计划未生成也未执行。

```bash
# 候选冻结与官网只读校验；零 Shijiu 请求
PYTHONPATH=src python scripts/import_shijiu_production_architecture_verification.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --prepare-only

# 全图片资源预检；严格零 Shijiu 请求/写入
PYTHONPATH=src python scripts/import_shijiu_production_architecture_verification.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --preflight-resources

# 每次调用最多一个 CREATE/UPDATE；终态 checkpoint 永久拒绝继续
PYTHONPATH=src python scripts/import_shijiu_production_architecture_verification.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --next-step \
  --confirm MIKIHOUSE_PRODUCTION_ARCHITECTURE_FINAL_E2E_SINGLE_STEP

# 从已有快照重建脱敏结论；零网络、零目标请求
PYTHONPATH=src python scripts/import_shijiu_production_architecture_verification.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --finalize-evidence-only
```

脱敏证据使用 `deliverables/shijiu_import/production_architecture_*.json`；其中 `production_architecture_final_html_forensics.json` 明确区分“API确认保存请求”与“目标端实际持久化状态”。只有完整HTML强回读通过时才会生成 `production_architecture_next_20_frozen_plan.json`，计划生成不代表获得执行授权。

## Shijiu 详情富文本保存契约（最新）

2026-09-04 已完成独立专项审计，不恢复或重试 `63-3210-146`，不创建任何 MIKIHOUSE 商品，不执行20件批次。严格只读抽样通过真实 UI-context Goods.index/getFormatInfo 检查了3件 `good_details` 非空商品：3件均为文本/轻量HTML（仅观察到 `p`/`section`/`h2`），`img` 和URL数均为0，而3件均使用独立 `good_detail_pics`。41次目标请求全部是只读，写入、上传和 legacy 修改均为0。

同一个新建的非 MIKI、非294884一次性测试商品完成了2次人工原生 EDIT 验证：

- 后台“详情介绍”以8字符纯文本进入 `good_details`，`/shopapi/Goods/newAddGood` 保存后 getFormatInfo 回读的类型、长度和 SHA-256 完全一致；
- 后台“详情图”添加1张已上传 Shijiu/COS 的图片时，`good_details` 哈希保持不变，图片只进入逗号分隔的 `good_detail_pics`，回读URL数、字符数和 SHA-256 完全一致；
- 两次都使用同一 `newAddGood` 完整保存 payload，未观察到独立详情保存接口；`good_describe` 是后台“简述”，`description` 未参与这两次详情编辑。

与冻结的 `63-3210-146` 精确对比显示：endpoint/query/headers/Content-Type 一致；原生 EDIT 与旧 MIKI writer 还存在 `virtual_sales`/`orderby`/`brand_id`、`id`/`original_price`/`tax_rate` 类型及顶层顺序差异，已如实记录，未将它们错当为已证明的拒绝原因。决定性业务值差异是旧最终 `good_details` 为2,250字符/3,156 bytes/16张内嵌图片，而原生图片流程始终保持轻量 `good_details`，并将图片单独存入 `good_detail_pics`。这证明可支持的生产表示，但不把1024或标签行为宣称为服务器硬限制。

新生产契约固定为：`good_details` 仅文本/轻量HTML，最多1024字符，禁止 `img` 和URL；全部详情图按序由 `good_detail_pics` 承载。生产 stage plan 已删除图片型 `FINAL_GOOD_DETAILS_HTML` UPDATE，保留轻量 CREATE、分段 broadcast 和分段 `good_detail_pics` full-payload UPDATE。`config/shijiu_richtext_contract.json` 是新的必查契约；脱敏证据为：

- `deliverables/shijiu_import/richtext_contract_readonly_audit.json`；
- `deliverables/shijiu_import/richtext_native_test_create_capture.json`；
- `deliverables/shijiu_import/richtext_native_text_edit_capture.json`；
- `deliverables/shijiu_import/richtext_native_image_edit_capture.json`；
- `deliverables/shijiu_import/richtext_contract_comparison.json`；
- `deliverables/shijiu_import/richtext_contract_readiness.json`。

readiness 仅冻结下一个新 MIKIHOUSE 端到端验证计划，不代表获得执行授权；本轮 MIKIHOUSE 写请求为0，351特殊品番仍在所有 Shijiu 阶段前置排除，legacy 286仅只读，所有历史冻结商品不重试，`shijiu_sku_id` 仍 nullable。

冻结商品 `10-9332-796` 获得单商品授权后使用独立入口执行，不复用任何历史冻结 checkpoint：

```bash
# 官网6 variants实时核验与独立checkpoint准备；无Shijiu写请求
PYTHONPATH=src python scripts/import_shijiu_richtext_e2e.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --prepare-only

# 18张资源全部下载、MIME/解码/尺寸/hash预检；仍为零Shijiu请求
PYTHONPATH=src python scripts/import_shijiu_richtext_e2e.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --preflight-resources

# 每次仅消费一个且不允许重试的 mutation stage；共5次，任一异常永久冻结
PYTHONPATH=src python scripts/import_shijiu_richtext_e2e.py \
  --browser-private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact \
  --next-step \
  --confirm MIKIHOUSE_RICHTEXT_CONTRACT_FINAL_E2E_SINGLE_STEP
```

执行器在每次 mutation 前重新核验官网 SKU/价格/库存/颜色/尺码/selectedOptions/variant图片，强制加载 `config/shijiu_richtext_contract.json`，并保证正式 payload 的 `good_details` 不含图片和URL。成功完成5阶段后才会生成 `richtext_e2e_next_20_frozen_plan.json`；计划不代表20件批次获得执行授权。

> 互斥审计：远端 `main` 在本轮执行期间新增 `AGENTS.md` 第14节。五阶段已完成目标端强回读，但写前没有取得可追溯的全局 production writer 互斥证据，因此只能认定为技术契约通过，不能认定生产治理 READY/COMPLETED。`richtext_e2e_writer_mutex_audit.json` 如实记录 `concurrent_shijiu_writer_observed=NOT_CAPTURED`，readiness 与20件计划均保持 `FAIL_CLOSED_NO_WRITE`。所有后续 `--next-step` 现在必须提供 Git 工作区外、与当前仓库/HEAD/商品/阶段精确匹配且未过期的 `--writer-mutex-evidence`，同时取得本机全局非阻塞 mutex；缺少任一证据会在任何 Shijiu 请求前停止。

本轮技术取证结果：官网实时核验 `10-9332-796` 的6个variant全部一致，税入价均为55,000 JPY，65折价均为35,750 JPY；18张官方图片完成HTTPS/域名/重定向/MIME/完整下载/解码/尺寸/hash预检且预检阶段Shijiu请求为0。目标端 `shijiu_product_id=9358340`，5阶段均完成强回读：18张有序broadcast、16张有序`good_detail_pics`、6个精确backend SKU、颜色/尺码/价格/库存/规格/主图/类目294884一致，405字符`good_details`在每阶段保持同一SHA-256且无图片和URL。请求台账为18次COS上传、1次CREATE、4次UPDATE、38次只读、0 failure、0 transport-unknown、`cross_source_writes=0`；`shijiu_sku_id`继续为null。

由于互斥证据缺失，`production_import_architecture_verified=false`。生成的20件代表性计划仅用于冻结审阅，状态为 `FROZEN_BLOCKED_MUTEX_EVIDENCE_NOT_CAPTURED`，不得执行。后续外部互斥证据的非敏感结构由 `config/shijiu_writer_mutex_evidence.schema.json` 定义；原值必须留在Git工作区外，并且每个写入阶段都要重新匹配当前仓库HEAD、商品和stage。一次性非MIKI富文本测试商品继续保留，不纳入本轮清理。

## Shijiu 首次初始化离线规划（当前）

`richtext_e2e_next_20_frozen_plan.json` 产生于稳定池新规则之前，现仅作为历史证据保留，状态固定为 `STALE_BUSINESS_RULE_CHANGED`、`must_never_execute=true`。相应 checkpoint 不得恢复，旧计划不能生成 writer 独占确认，`scripts/import_shijiu_pilot_20.py` 会在任何网络或目标请求前 fail closed。

`scripts/plan_shijiu_stable_initialization.py` 是首次初始化的纯离线规划入口。它只读取已完成的完整官网 source snapshot、压缩 `stable_catalog`、351特殊名单、mapping、历史冻结状态及已验证价格/富文本配置，不创建网络 client，也没有 live-write 参数：

```bash
PYTHONPATH=src .venv/bin/python scripts/plan_shijiu_stable_initialization.py
```

入口执行以下 fail-closed 检查：完整官网分页证明；stable/source 单商品价格、库存、variant和图片指纹一致；351特殊名单数量；固定类目294884；`ceil(tax_included_price_jpy × 0.65)`；`availableForSale` 到0/1库存；`good_details` 文字/轻HTML且无图片、无URL；已映射与全部历史尝试/冻结品番禁止 CREATE。每件可计划商品保存 `CREATE_CORE`、每步最多8张的broadcast UPDATE、每步最多8张的`good_detail_pics` UPDATE、nullable `shijiu_sku_id`、脱敏payload SHA-256及只含官方source URL/hash/顺序的资源manifest。规划过程不下载图片、不上传COS，也不生成mutex evidence。

新输出位于 `deliverables/shijiu_initialization/`：

- `stable_pilot_20_frozen_plan.json`：当前stable pool中新选的20件代表性冻结pilot，footwear/apparel/baby/goods各5件；
- `stable_initialization_batch_plan.json.gz`：逐商品stage、variant、资源manifest及分层批次的完整计划；
- `stable_initialization_batch_summary.json`：按简单、普通、多SKU、富媒体、高复杂度排序的批次摘要；
- `stable_initialization_data_quality_audit.json`：2461件的价格、variant、图片、名称、SKU与identity审计；
- `duplicate_good_name_offline_audit.json`：重复名称组、完整backend SKU集合唯一性及理论解锁数量；
- `duplicate_good_name_shijiu_readonly_validation.json`：10组真实UI-context只读候选和强身份验证；
- `price_outside_configured_range_audit.json`：37个越界variant的0价/真实高价分类，保持guard不变；
- `stable_initialization_capacity_estimate.json`：未来CREATE/UPDATE/COS/readback工作量估算；
- `stable_initialization_readiness.json`：freshness、交接和写入阻塞结论；
- `state/mikihouse_initialization_checkpoint.json.gz`：只含 `FROZEN_PLANNING_ONLY` 的批次/商品初始checkpoint，mutation计数为0。

`good_name` 重复不再被当成不可导入条件。新契约固定为：真实UI-context Goods.index的精确名称查询只缩小候选product ID集合；随后对每个候选调用getFormatInfo，完整 `MIKI-<variant SKU>` 集合是主要强身份，同时要求类目294884、variant数量、规格结构和逐variant价格一致。只有一个完整匹配才返回 `UNIQUE_STRONG_MATCH`；零匹配返回 `NOT_FOUND`，多个完整匹配返回 `AMBIGUOUS`，后二者都禁止binding。列表顺序、创建时间、模糊名称、相似价格及单SKU重合永远不能作为身份依据。正式目标variant身份仍为 `shijiu_product_id + exact backend_sku_code`，`shijiu_sku_id=null`。

当前stable catalog的1603件重复名商品分属254组，最大组“半袖Ｔシャツ”为76件；所有backend SKU在source内全局唯一，且不存在两个不同product_number拥有完全相同完整SKU集合，理论可解除仅由重名造成的1603件复核。真实Shijiu只读验证覆盖10组，包括2件小组、“セーター”及“トレーナー”“カバーオール”“セカンドベビーシューズ”“パンツ”“半袖Ｔシャツ”等大组：21次请求全部仅为 Goods.index/getFormatInfo；已映射 `63-6602-492` 在9件同名source商品中取得唯一完整SKU强匹配，其余325件未创建商品均正确返回 `NOT_FOUND`，没有误绑legacy/foreign商品。CREATE/UPDATE/COS/上下架/价格库存写入和mapping修改均为0。

重新规划后，2461件全部有且仅有一个初始化处置：2385件通过数据质量门禁并拆为170个隔离批次；6件已有经过验证的MIKIHOUSE mapping，禁止再次CREATE并交给增量引擎；43件属于历史尝试/冻结集合；27件保留初始化复核。真正剩余问题仅为7件缺图和21件价格越界商品（集合有1件重叠），不再包含 `DUPLICATE_PRODUCT_NAME`。新20件pilot仍全部来自当前STABLE、未映射、非历史冻结池，footwear/apparel/baby/goods各5件且每件都携带新强身份回读契约。

价格越界审计保持原guard `1..1,000,000 JPY` 不变：34个variant/20件商品的官网税入价为0，继续禁止凭空生成售价；另3个variant属于同一件稳定的143万JPY金标高价商品，分类为“可能是真实高价、旧上限可能过窄，但必须单独人工确认和业务授权”。本轮自动释放数量为0，没有为了增加可导入数调整guard。

只读验证命令（只接受 Git 工作区外的既有browser证据目录）：

```bash
PYTHONPATH=src .venv/bin/python scripts/audit_shijiu_duplicate_good_name.py \
  --private-dir /absolute/outside/repo/.secrets/shijiu-browser-exact
```

未来执行任一pilot或批次前，必须完成新的全站crawl并用 stable catalog/source snapshot 顶层hash及逐商品指纹检查 freshness。商品若变为特殊、WEB限定、促销、稳定性待复核或inactive，在任何目标动作前冻结；价格、库存、variant或图片变化则整件重新生成stage payload。初始化完成并取得 verified MIKIHOUSE mapping 后，由交接函数将商品登记到现有 source sync state，此后只接受增量事件，禁止定时任务再次CREATE。同一checkpoint重跑幂等；批次失败只冻结当前批次，后续批次不被污染。

2026-09-05 当前 WAWU 仍可能在同一正式租户写入，因此所有新计划状态均为 `PLANNING_ONLY / SHIJIU_WRITE_BLOCKED_CONCURRENT_WRITER`，`execution_authorized=false`。本轮只执行了上述21次明确授权的Shijiu只读请求；CREATE、UPDATE、COS生产上传、上下架、价格库存写入及writer mutex evidence生成均为0，legacy286未被修改。
