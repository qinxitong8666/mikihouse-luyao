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

- `master_catalog.json`：长期保存的商品主库，包含商品、颜色图片和完整 variants；
- `products.csv`、`variants.csv`：标准化商品表和 variant 表（UTF-8 BOM）；
- `incremental_changes.json`、`incremental_changes.csv`：本次新增、价格、库存、图片、元数据、下架及恢复变化；
- `crawl_stats.json`：全站分页、排除、商品/variant、图片和库存统计；
- `validation_report.json`：鞋类、服装、婴儿用品、杂货的真实样例校验。

`special_skus_2026aw.csv` 的 351 个品番是永久排除集合，采集时即从小程序候选池移除。普通商品按每个 variant 独立计算：

```text
mini_program_price_jpy = ceil(官网税入日元价 × 0.65)
```

该字段仍是日元整数，本模块不保存人民币售价、汇率或任何人民币换算结果。商品以 `product_number`、variant 以 `product_number::variant SKU` 为稳定标识。只有全站分页完整成功并通过跨品类、特殊品番和价格校验后才会原子更新主库；官网不再返回的商品或 variant 保留历史记录并标记 `active=false`，以后重新出现时记录为恢复上架。

统计会分别记录排除集合总数、本次官网实际遇到并排除的数量，以及当前官网未出现的特殊品番数量。即使特殊品番暂时下架，仍永久保留在排除集合中，恢复上架后也不会进入小程序商品池。

为便于 GitHub 审核，脚本同时把体积较小的抓取统计、增量变化摘要和分类验证报告写入 `deliverables/storefront_catalog/`；摘要包含完整变化文件的路径、大小、SHA-256 及代表样例。完整主库、逐条变化 JSON/CSV 和同步 CSV 属于运行数据，继续由 `.gitignore` 排除；本模块不会调用 PDF 生成器，也不会修改现有 2026AW PDF 成品。

## Shijiu importer discovery 与 dry-run

本项目的最终目标端是 **Shijiu（世九）小程序后台**。`qinxitong8666/wawu-product-sync` 仅作为其中可明确定位到 Shijiu 的下游 client、字段样例、回读、checkpoint/resume、回滚和批处理安全机制的参考；瓦屋上游 API、瓦屋 mapper、瓦屋价格/分类/SKU 语义均未复用。证据边界和当前 main 缺失的真实成功写入证据详见 `docs/shijiu_downstream_contract_audit.md`。

当前 adapter 刻意不包含任何写方法，也没有写入 CLI 参数。Shijiu 客户端只允许商品/详情/分类 discovery 所需的语义只读端点：

- `/shopapi/Goods/index`：按稳定 SKU code 查重；
- `/shopapi/goods/getFormatInfo`：已存在商品回读；
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

持久映射表为 `state/shijiu_mappings.json`，为每个 product number 和 variant SKU 建立独立行；尚未写入的 Shijiu `product_id`/`sku_id` 保持 `null`，不得猜测。目标 discovery 只接受已持久绑定或精确 `MIKI-<variant SKU>`，商品名匹配被明确禁止。

只读分类树确认 Shijiu 已有子类目 `MikiHouse`（ID `294884`，父类目 `母婴用品` ID `288338`）。所有可发布 MIKIHOUSE 商品固定使用 `good_type=294884`，不得按官网品牌或分类散落到其他 Shijiu 类目。官网 `brand`、`productType`、`category`、`tags` 只保存在 source metadata；由于没有已验证的 Shijiu 品牌 discovery 契约，`brand_id` 和 `supplier` 保持空值。

每个 variant 的 `sku_price` 和会员价格直接复制现有 `mini_program_price_jpy`，并再次验证其等于 `ceil(官网税入日元价 × 0.65)`；币种保持 JPY，不做人民币或汇率换算。Storefront 只提供 `availableForSale` 而没有库存件数，因此目标 `sku_stock` 保守映射为可售 `1`、不可售 `0`，审计数据同时保留原始布尔状态和来源说明。

每次全站抓取继续由 catalog 模块以上一次 master catalog 为基线，按 variant SKU 输出独立的 `NEW_PRODUCT`、`NEW_VARIANT`、`PRICE_CHANGED`、`INVENTORY_CHANGED`、`IMAGE_CHANGED`、下架及恢复事件。只有 `PRICE_CHANGED` 能生成 `UPDATE_PRICE_BY_EXACT_VARIANT_SKU`，且不会重新创建商品。价格保护配置位于 `config/shijiu_price_guard.json`：新价格越界，或绝对/相对变化超过阈值时只写入 `review_required.json`，不得进入自动更新计划。

完整字段预览、增量操作、checkpoint 和 Shijiu 只读快照写入 `output/shijiu-import/`；可追踪的 2603 项精简动作计划、增量摘要、review required、20 个字段样例、价格校验、映射表、契约审计和只读验证写入仓库。payload 是字段映射预览而不是可执行写请求：图片保留 MIKI 官网来源，未来需在另行授权的阶段通过已定位的 Shijiu COS 上传接口取得目标 URL。缺少官网图片的商品会设置 `publish_ready=false` 并跳过，绝不使用其他商品图片替代。当前 adapter 即使设置写入环境变量也会拒绝运行。
