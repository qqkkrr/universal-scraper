# examples

- `legacy/ggzy_crawl.py` / `legacy/ggzy_details.py`：早期脚本式版本（已废弃）。

**现在请用配置驱动引擎**（配置模板用 `scaffold` 生成，仓库不预置历史 config）：
```bash
python3 -m universal_scraper.cli scaffold --name ggzy_demo
python3 -m universal_scraper.cli run --config configs/new_task.json
```
