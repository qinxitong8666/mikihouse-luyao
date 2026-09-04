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

持久映射表为 `state/shijiu_mappings.json`，为每个新 product number 和 variant SKU 建立独立行；尚未完成未来“创建后回读”的 Shijiu `product_id`/`sku_id` 保持 `null`，不得猜测。目标商品 ID 只允许来自以后另行授权的新建成功回读，商品名匹配和对旧商品做 SKU reconciliation 均被明确禁止。

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
- 首次批次执行器强制 `state=0`、`is_shelf=0`；受控恢复则严格采用已审计原生样例的 `state=1`、`is_shelf=0`，两者都不改变“默认下架/不可见”业务要求；
- 每个上传和创建请求前写 checkpoint；已完成上传可以恢复，结果不明的上传或创建禁止自动重试；
- 创建后必须回读并验证商品 ID、SKU ID、类目、价格、库存、规格、主图、轮播和详情；验证通过后才原子更新 `state/shijiu_mappings.json`；
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

2026-09-04 已完成一次真实 browser-exact 验证。首次新增请求因登录后在新标签打开后台而未被旧版单页监听器捕获；修复为 context 级监听后，只对同一非 MIKIHOUSE 一次性测试商品执行一次人工编辑保存并成功捕获。脱敏结果为 `BROWSER_EXACT_PRODUCT_VERIFIED_SKU_ID_NOT_EXPOSED`：

- 当前持久化成功的原生编辑请求没有 Cookie/Authorization，query token 与 body token 均非空且完全相同；因此“MIKIHOUSE 请求缺少 Cookie”已被排除为充分根因；
- `Goods.index` 在后台页面的真实读取上下文中唯一回读 `product_id=9357918` 和相同商品名，`getFormatInfo` 回读相同商品及 1 个 SKU 的完整价格、库存、规格和图片结构；
- 当前 `getFormatInfo.data.sku_info[]` 不含 `id`、`sku_id`、`goods_sku_id` 或 `good_sku_id`。这一事实与 6 个 legacy 样本共 70 个 SKU 以及 `wawu-product-sync@a36c5ea` 的明确注释一致，故 `sku_id` 必须保持 null，绝不从数组位置、规格或商品 ID 猜测；
- 当前原生编辑请求相对历史 WAWU/此前 MIKIHOUSE payload 新增 `id/orderby/virtual_sales`、缺少 `brand_id`，且 `original_price`、`tax_rate`、`sku_stock` 的类型不同；body 顺序不同，browser 请求还包含 `Origin`。由于捕获的是同一商品的 EDIT 而非首次 CREATE，这些是下一轮离线 mapper 审计的候选差异，不足以直接宣称 CREATE 静默拒绝根因。

恢复回读支持 `--mode readback`（审计 client 直连）和 `--mode ui-readback`（复用私有后台页面的真实 `Goods.index` 身份）；`--mode finalize` 只从 Git 外已有证据重建脱敏报告，网络请求为零。最终证据见 `deliverables/shijiu_import/browser_exact_capture_readiness.json` 和 `browser_exact_capture_analysis.json`。本轮 MIKIHOUSE 写请求、自动商品写请求和 legacy 操作均为 0。
