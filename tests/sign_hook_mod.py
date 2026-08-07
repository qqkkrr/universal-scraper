"""测试用签名钩子：给请求加 X-Sign 头。"""
def add_sign(headers, url, body):
    headers["X-Sign"] = "ok"
    return headers
