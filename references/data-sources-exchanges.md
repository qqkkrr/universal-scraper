# 全球主要交易所数据源起点索引（batch2200 战训产物）

> **定位**：每条只给"官网 + 数据页路径模式 + 格式 + 语言"作为**侦察起点**——
> 端点会改版，动手前先在官网内确认当前入口（首页 → Market Data / Statistics / Products）。
> 不确定的一律标 ⚠️。**小型交易所的日度数据经常只存在于付费终端或根本不上网**——
> 这是常态不是故障，按 playbook 六·一 判 nodata，别烧轮次。

## 判别总则（先读）

1. 交易所数据三条通用通道：①官网行情页（多为 JS 壳→capture 抓接口）②官方"统计/月报"
   静态文件（CSV/XLSX/PDF，直接下载）③指数公司页面（富时罗素/标普道琼斯/MSCI 只给
   成分权重，价格走交易所）。
2. 历史回溯窗口各源不同（多数官网行情只留近期，官方月报最全）——历史日期取空≠失败。
3. 时区与交易日历：交易所日期按当地时区；节假日当天"无数据"是正常态。

## 亚太

| 交易所 | 数据起点 | 格式/备注 |
|---|---|---|
| 上交所 sse.com.cn | 官网"行情与数据"→ 统计月报；上证50 等指数权重在"指数"栏目 | XLSX/HTML，中文，境内网络友好 |
| 深交所 szse.cn | "市场数据"→ 统计资料；深证100 同路径 | XLSX |
| 北交所 bse.cn | "市场数据" | XLSX，数据粒度较粗 |
| 港交所 hkex.com.hk | "Market Data"→ Securities Daily Quotation / 披露易 hkexnews.hk | HTML/CSV，繁中+英文 |
| 台湾证交所 twse.com.tw | "Statistics"→ 每日市场成交 / TWSE 成分；API: mis.twse.com.tw（⚠️ 非官方、限流严） | JSON/CSV |
| 韩交所 kreb.or.kr / krx.co.kr | "Statistics"→ KOSPI200 数据（英文站 data.krx.co.kr 提供下载） | CSV，需选英文界面 |
| 东证所 jpx.co.jp | "Statistics"→ 每日/月度统计；日经225 成分走 nikkei（指数公司页） | HTML/CSV，日英双语 |
| 新加坡交易所 sgx.com | "Market Data"→ sgx.com/research-education/publications（月度统计包） | XLSX |
| 澳交所 asx.com.au | "Market Data"→ 统计资源页；ASX200 成分在"Prices & Research" | CSV/XLSX，英文 |
| 澳洲另有 NZX nzx.com | "Markets"→ Statistics | CSV |

## 美洲

| 交易所 | 数据起点 | 格式/备注 |
|---|---|---|
| 纽交所 nyse.com | "Market Data"→ NYSE Daily Market Statistics / Initial Listing | XLSX/PDF，英文 |
| 纳斯达克 nasdaq.com | "Market Activity"→ Initial Public Offerings / nasdaqdatallc（API 需注册） | HTML/JSON |
| 多伦多交易所 tsx.com | "Market Intelligence"→ TSX/TSXV 统计（月度 XLSX 很全） | XLSX |
| 巴西 B3 b3.com.br | "Dados de Mercado"→ 统计序列（葡语界面有英文切换） | CSV |

## 欧洲/中东/非洲

| 交易所 | 数据起点 | 格式/备注 |
|---|---|---|
| 伦敦证交所 lseg.com | "Investors/Market data"→ LSEG 统计报告（月度 XLSX） | XLSX，英文 |
| 法兰克福（德交所）deutsche-boerse.com | "Market Data"→ DAX 指数页 + 统计（XETRA 数据） | CSV/HTML，德英双语 |
| 泛欧 euronext.com | "Markets"→ Statistics（ covered 成分与行情下载） | CSV |
| 瑞士 SIX six-group.com | "Market Data"→ 统计报告 | XLSX，德英 |
| 斯德哥尔摩 nasdaqomxnordic.com | 北欧市场统一入口（Nasdaq Nordic Statistics） | CSV/XLSX |
| 莫斯科交易所 moex.com | "Markets"→ Statistics（俄语界面，英文站 moex.com/en）——⚠️ 受制裁影响部分数据停止对外发布 | CSV/HTML |
| 伊斯坦布尔 borsaistanbul.com.tr | "Data"→ 统计（土英双语） | XLSX |
| 约翰内斯堡 jse.co.za | "Market Data"→ Statistics | CSV/XLSX |
| 特拉维夫 tase.co.il | "Market Data"（英希伯来双语） | HTML/CSV |

## 通用兜底（当交易所官网拿不到时）

- **指数公司**：STOXX、FTSE Russell、MSCI、标普道琼斯指数官网都有成分/权重
  公开页（价格通常不给，权重+日期给）。
- **聚合器**：investing.com / tradingeconomics / stoq / Stooq（stooq.com 免费 CSV，
  覆盖多数主要指数历史日线）——⚠️ 第三方聚合器数据用于**核对**，交付以官方为准。
- **WebSearch 判 nodata**：小交易所（瓦努阿图、太平洋群岛级）先搜索确认
  "数据是否存在于公开互联网"，不存在 → `batch nodata`，见 playbook 六·一。
