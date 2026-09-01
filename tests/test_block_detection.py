"""detect_block 误判回归：CDN 透传的 200 + cf-ray 不得误判为 Cloudflare 拦截。
背景：DockerHub/V2EX 等站走 Cloudflare CDN，正常 200 也带 cf-ray，曾误杀导致限流误报。"""
from universal_scraper.antibot import detect_block

BIG_BODY = "<html><body>" + "正常内容" * 1000 + "</body></html>"


def test_cdn_passthrough_200_with_cf_ray_not_blocked():
    d = detect_block(200, BIG_BODY, {"cf-ray": "8f0aeaa"}, "https://hub.docker.com/v2/x")
    assert d["kind"] == "none", f"CDN 透传被误判: {d}"


def test_cf_header_plus_tiny_body_is_challenge():
    d = detect_block(200, "<html>ok</html>", {"cf-ray": "8f0aeaa"}, "https://x.example/")
    assert d["kind"] == "cloudflare"


def test_just_a_moment_still_blocked():
    d = detect_block(200, "Please wait... Just a moment...", {}, "https://x.example/")
    assert d["kind"] == "cloudflare"


def test_article_mentioning_cloudflare_not_blocked():
    body = "<html><body><p>本文介绍 cloudflare workers 部署实践</p>" + "技术内容" * 800 + "</body></html>"
    d = detect_block(200, body, {}, "https://blog.example/cloudflare-workers-guide")
    assert d["kind"] == "none", f"正文提及 cloudflare 被误判: {d}"


def test_real_block_markers_still_work():
    assert detect_block(200, "cf-challenge in progress", {}, "")["kind"] == "cloudflare"
    assert detect_block(403, "", {}, "")["kind"] == "403"
    assert detect_block(200, "请完成安全验证", {}, "")["kind"] in ("waf", "verify")
    assert detect_block(429, "", {}, "")["kind"] == "429"
