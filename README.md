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

正式 2026AW 批次只需将完整特殊品番逐行写入同一 CSV。程序顺序处理所有品番、缓存已经下载的图片，并在单个品番失败时继续余下任务。

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

可通过 `--json`、`--pdf`、`--failures` 和 `--image-cache` 修改路径。在线抓取优先使用官网公开的只读 Storefront API，商品 HTML 页面作为回退。公开令牌轮换时可用 `MIKIHOUSE_STOREFRONT_TOKEN` 环境变量覆盖。

批量任务逐 SKU 隔离失败：单商品抓取或图片下载失败不会阻断其他商品。只要仍有成功商品，就会生成 JSON 和 PDF；失败项写入 `failed_skus.json`，进程返回码为 `2`，便于自动化任务识别“部分成功”。全部成功返回 `0`，全失败返回 `1`。

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
- 主图先下载并校验为有效图片，再从本地缓存嵌入 PDF，避免生成时出现远程图片空白。
- 页面结构变化时会快速失败，避免静默产生错误价格表。
