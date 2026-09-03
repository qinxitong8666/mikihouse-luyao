# mikihouse-luyao

第一阶段的 MIKI HOUSE 商品抓取与 PDF 价格表骨架。程序读取 `special_skus.csv`，按品番访问 MIKI HOUSE 日本官网商品页，校验商品编号并提取商品名、税入价、官方主图、颜色、尺码和库存；同时计算 PDF 售价并输出 JSON 与基础 PDF 价格表。

## 定价规则

```text
PDF售价 = ceil(税入价 × 0.73 × 0.0435)
```

例如 `10-1105-495` 的税入价为 `¥44,000`，PDF 售价为 `1398`。

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

## 运行

```bash
mikihouse-luyao --input special_skus.csv
```

等价的模块命令：

```bash
python -m mikihouse_luyao --input special_skus.csv
```

默认输出：

- `output/products.json`
- `output/pdf/mikihouse_price_list.pdf`

可通过 `--json` 和 `--pdf` 修改路径。官网可能对数据中心 IP 返回 HTTP 403；这是站点侧的访问限制，程序会重试后明确失败。请降低运行频率并遵守官网条款，勿绕过访问控制。

## 可复现的端到端样例

仓库保存了从官方页面 JSON-LD 精简得到的 `10-1105-495` 测试夹具，因此无需联网也能验证 CSV → 抓取解析 → 校验 → 定价 → JSON/PDF 的完整链路：

```bash
python -m mikihouse_luyao \
  --input special_skus.csv \
  --html-file tests/fixtures/10-1105-495.html \
  --json output/products.json \
  --pdf output/pdf/mikihouse_price_list.pdf
```

## 当前范围

- 以官网 `ProductGroup` JSON-LD 为主要数据源，页面库存文字补充“剩余 N 件”等精确信息。
- 校验请求品番与官网 canonical 商品 URL 一致，并检查必填字段和各变体价格一致性。
- 第一阶段 PDF 是便于核对的数据表，不下载/嵌入远程主图；JSON 中保留完整官方主图 URL。
- 页面结构变化时会快速失败，避免静默产生错误价格表。
