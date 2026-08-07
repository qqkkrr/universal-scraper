# 第二代：100 个贴合实际的困难爬虫测试靶场（本地可控）

## 这是什么
把 100 个贴合真实网站的反爬场景做成**本地可控靶场**（`challenges2/server.py`，端口 8756），
机制与真实厂商等价，但 100% 可自动复现：
腾讯防水墙/极验语序/旋转验证码/中文算术/reCAPTCHA v3/Stackpath/Base64 验证码/SVG 轨迹/
接码平台/缺口拼图/Cloudflare 5秒盾/Shape VM/微信文章/小程序私有帧/支付宝环境/抖音短链/
B站极验/小红书签名/知乎倒立文字/京东淘宝滑块/拼多多 anti_content/美团签名/58 验证码/
瑞数/数美/Akamai/F5/Imperva/Distil/CloudFront WAF/多层加密/MessagePack/Chunked/QUIC/
头顺序/Cookie 哈希/WS DH/MetaMask/WebAuthn/SW 篡改/Worker 哈希/WASM/时间侧信道/
属性劫持/反调试/rAF Canvas/轨迹/指纹/扩展检测/hasFocus/postMessage/CSP nonce/foreignObject/
语言差异/HMAC/加密分页/WS 帧解密/阿里网关/腾讯云 WAF/动态字体/CSS counter/attr(Base64)/
HLS/MIME/TOTP/hCaptcha/Arkose/FunCaptcha/Reddit 限流/Shopify/Zendesk/__cf_bm/Gmail/JWT/
SAML/GraphQL/JSON 劫持/user-select/窗口尺寸/快速关闭 WS/网络类型/蜜罐/懒加载/webdriver/
SharedArrayBuffer/AudioContext/performance.memory/PBKDF2/deviceMemory/getBattery/预检/
Sec-GPC/screenX/Kotlin/Emscripten/混合模式/DTMF/Digest/综合 CTF。

## 运行
```bash
export CHALLENGE_MOD=challenges2
python3 tests/challenge_runner2.py          # 全部 100 题
python3 tests/challenge_runner2.py 1 50 100 # 指定题目
```
报告：`challenges2/results.json`（由 runner2 输出到 challenges2/results.json 可自行重定向）

## 诚实说明
- 商业验证码（腾讯/极验/B站/淘宝/京东/Discord/Arkose/LinkedIn 等）用**机制等价的可控实现**；
- 需要真实外部条件（真 QUIC、真 WebAuthn 认证器、真 IMAP/Gmail、真实钱包）的用**等价模拟**；
- 全部 100 题已由万能爬虫工具全自动攻克（结果见运行输出）。
