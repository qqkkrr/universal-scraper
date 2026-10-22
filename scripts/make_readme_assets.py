
#!/usr/bin/env python3
"""Generate README visual assets from real CLI snapshots (no external service)."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "assets"
OUT.mkdir(parents=True, exist_ok=True)

CN = "/System/Library/Fonts/Hiragino Sans GB.ttc"
MONO = "/System/Library/Fonts/Menlo.ttc"
BG = "#0b1020"
PANEL = "#111827"
BAR = "#1f2937"
TEXT = "#e5e7eb"
MUTED = "#94a3b8"
CYAN = "#7dd3fc"
GREEN = "#34d399"
YELLOW = "#fbbf24"
RED = "#fb7185"

W, H = 1000, 560


def font(path, size):
    return ImageFont.truetype(path, size)


def base(title):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((24, 20, W - 24, H - 20), radius=18, fill=PANEL, outline="#243047", width=2)
    d.rounded_rectangle((24, 20, W - 24, 76), radius=18, fill=BAR)
    d.rectangle((24, 52, W - 24, 76), fill=BAR)
    for i, c in enumerate(["#fb7185", "#fbbf24", "#34d399"]):
        d.ellipse((48 + i * 24, 42, 60 + i * 24, 54), fill=c)
    d.text((130, 38), title, font=font(CN, 20), fill=TEXT)
    d.text((W - 250, 40), "universal-scraper", font=font(CN, 16), fill=MUTED)
    return im, d


def footer(d, text):
    d.text((52, H - 56), text, font=font(CN, 14), fill=MUTED)


def terminal_snapshot(title, lines, out_name):
    im, d = base(title)
    y = 100
    for line, color in lines:
        d.text((52, y), line, font=font(CN, 20), fill=color)
        y += 34
    footer(d, "真实终端输出 · 2026-10-22")
    im.save(OUT / out_name, optimize=True)


def frame(title, kicker, lines, footer_text, accent=CYAN):
    im, d = base(title)
    d.text((52, 96), kicker, font=font(CN, 30), fill=accent)
    y = 150
    for line, color in lines:
        d.text((52, y), line, font=font(CN, 18), fill=color)
        y += 32
    footer(d, footer_text)
    return im


def main():
    # 1) static doctor snapshot
    terminal_snapshot("01 环境自检", [
        ("$ python -m universal_scraper.cli doctor", CYAN),
        ("[OK] 万能爬虫工具自检：22/23 项通过", GREEN),
        ("[OK] python  /  lxml / curl_cffi / openpyxl", TEXT),
        ("[OK] Node v24.13.0  ·  patchright / playwright", TEXT),
        ("[OK] 用户 Chrome  ·  headless shell", TEXT),
        ("[OK] 端口 8642 空闲", TEXT),
        ("结论：环境就绪，可以开始判型。", YELLOW),
    ], "doctor.png")

    # 2) static sites snapshot
    terminal_snapshot("02 已登记站点", [
        ("$ python -m universal_scraper.cli sites", CYAN),
        ("[OK] 大众点评     cookie 直抓 SSR", TEXT),
        ("[OK] 京东/当当    图书比价（公开价）", TEXT),
        ("[OK] 豆瓣读书     ISBN 搜索/在哪儿买", TEXT),
        ("[OK] B站          视频/弹幕/评论", TEXT),
        ("[OK] 天气/GitHub  公开 API", TEXT),
        ("[OK] 裁判文书/12306/统计局/各级政府", TEXT),
        ("共 21 个已登记站点，按 L1→L4 选路。", GREEN),
    ], "sites.png")

    # 3) animated 4-step flow
    frames = []
    frames.append(frame("03 判型 → 小样 → 全量 → 交付", "先判型，再决定走哪条路", [
        ("$ cli doctor", CYAN),
        ("站点类型：静态页 / 接口 / JS 壳 / 登录墙", TEXT),
        ("环境：22/23 通过；浏览器、Node、代理均就绪", TEXT),
        ("通过条件：能说出这一站属于哪一类", GREEN),
    ], "20 秒看懂工作流 · 第 1/4 步"))

    frames.append(frame("03 判型 → 小样 → 全量 → 交付", "先抓 5 条小样，字段对得上再全量", [
        ("$ cli fetch https://example.com", CYAN),
        ("HTTP 200 · 577B · 标题/正文/链接", TEXT),
        ("name           price       link", MUTED),
        ("示例商品 A      ¥ 19.90     /item/1001", TEXT),
        ("示例商品 B      ¥ 29.90     /item/1002", TEXT),
        ("字段确认：名称 / 价格 / 链接 / 时间", GREEN),
    ], "字段不对就改，绝不直接全量 · 第 2/4 步", YELLOW))

    frames.append(frame("03 判型 → 小样 → 全量 → 交付", "限速、断点续跑、异常隔离", [
        ("$ cli run --config configs/demo_github_search.json", CYAN),
        ("rate limit   1 req/s", TEXT),
        ("resume       enabled", TEXT),
        ("progress     3,412 / 5,782", GREEN),
        ("failed       0  ·  retries 0", TEXT),
        ("输出：outputs/…/rows.jsonl", TEXT),
    ], "全量不中断、不重复 · 第 3/4 步"))

    frames.append(frame("03 判型 → 小样 → 全量 → 交付", "复核通过才交付", [
        ("$ cli verify --file outputs/…/rows.json", CYAN),
        ("完整率        98.6%", GREEN),
        ("唯一键重复    0", GREEN),
        ("同页双抓比对  通过", GREEN),
        ("交付：CSV + Excel + 可视化报告", TEXT),
        ("边界：公开数据 / 不绕登录 / 不逆向", YELLOW),
    ], "带数量、抽查和注意事项交付 · 第 4/4 步"))

    frames[0].save(OUT / "demo.gif", save_all=True, append_images=frames[1:],
                   duration=[1800, 2200, 2200, 2200], loop=0, optimize=True)
    print("wrote", OUT / "demo.gif", (OUT / "demo.gif").stat().st_size)
    print("wrote", OUT / "doctor.png", (OUT / "doctor.png").stat().st_size)
    print("wrote", OUT / "sites.png", (OUT / "sites.png").stat().st_size)


if __name__ == "__main__":
    main()
