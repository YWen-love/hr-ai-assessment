"""验证链接 host 自动跟随请求来源 + 404 友好 HTML"""
import urllib.request, urllib.parse, urllib.error
import io, time, json, mimetypes
from pathlib import Path

BASE = "http://127.0.0.1:7860"
SAMPLE = Path(r"C:\Users\15219\AI项目\resumes_demo\data\张三简历.pdf")

def upload(candidate, job, host=None, xfp=None, xfh=None):
    boundary = "----b" + str(int(time.time()))
    body = io.BytesIO()
    def field(name, value, filename=None, ctype=None):
        body.write(f"--{boundary}\r\n".encode())
        if filename:
            body.write(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
            body.write(f"Content-Type: {ctype or 'application/octet-stream'}\r\n\r\n".encode())
            body.write(value if isinstance(value, bytes) else value.encode())
            body.write(b"\r\n")
        else:
            body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            body.write(value.encode())
            body.write(b"\r\n")
    field("candidate_name", candidate)
    field("job_name", job)
    field("file", SAMPLE.read_bytes(), filename=SAMPLE.name,
          ctype=mimetypes.guess_type(SAMPLE.name)[0] or "application/pdf")
    body.write(f"--{boundary}--\r\n".encode())
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if host: headers["Host"] = host
    if xfp: headers["X-Forwarded-Proto"] = xfp
    if xfh: headers["X-Forwarded-Host"] = xfh
    req = urllib.request.Request(f"{BASE}/hr/parse-link", data=body.getvalue(),
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def http_get(path, accept="text/html", host=None, xfp=None, xfh=None):
    headers = {"Accept": accept}
    if host: headers["Host"] = host
    if xfp: headers["X-Forwarded-Proto"] = xfp
    if xfh: headers["X-Forwarded-Host"] = xfh
    req = urllib.request.Request(f"{BASE}{path}", method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers.get("Content-Type", "?"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "?"), e.read()

# ─── 测试 1：本地 Host 头 → 链接应当是 http://127.0.0.1:7860/candidate/... ───
print("\n" + "=" * 70)
print("  测试 1: 本地请求（Host: 127.0.0.1:7860）")
print("=" * 70)
code, data = upload("李四", "人力实习生", host="127.0.0.1:7860")
print(f"  HTTP {code}  session_id: {data.get('session_id')}")
print(f"  public_link: {data.get('public_link')}")
expect_1 = "http://127.0.0.1:7860/candidate/"
ok_1 = (data.get("public_link", "").startswith(expect_1))
print(f"  {'✓' if ok_1 else '✗'} 期望以 '{expect_1}' 开头 → {'PASS' if ok_1 else 'FAIL'}")

# ─── 测试 2：模拟魔搭反代（X-Forwarded-Proto: https, X-Forwarded-Host: ms.show）──
print("\n" + "=" * 70)
print("  测试 2: 模拟魔搭反代（X-Forwarded-Proto/Host）")
print("=" * 70)
code, data = upload("王五", "人力实习生",
                    host="ywen666-hr-ai-assessment.ms.show",
                    xfp="https", xfh="ywen666-hr-ai-assessment.ms.show")
print(f"  HTTP {code}  session_id: {data.get('session_id')}")
print(f"  public_link: {data.get('public_link')}")
expect_2 = "https://ywen666-hr-ai-assessment.ms.show/candidate/"
ok_2 = (data.get("public_link", "").startswith(expect_2))
print(f"  {'✓' if ok_2 else '✗'} 期望以 '{expect_2}' 开头 → {'PASS' if ok_2 else 'FAIL'}")

# ─── 测试 3：404 返回 HTML 而不是 JSON ───
print("\n" + "=" * 70)
print("  测试 3: 不存在的 session_id → 404 + HTML")
print("=" * 70)
code, ctype, body = http_get("/candidate/00000000-0000-0000-0000-000000000000", accept="text/html")
print(f"  HTTP {code}  Content-Type: {ctype}  size: {len(body)}B")
text = body.decode("utf-8", errors="replace")
is_html = "text/html" in ctype
shows_friendly = "测评链接已失效" in text
no_json_detail = '"detail"' not in text or '"detail": "Session not found"' not in text
print(f"  {'✓' if is_html else '✗'} Content-Type 是 HTML")
print(f"  {'✓' if shows_friendly else '✗'} 含友好文案'测评链接已失效'")
print(f"  {'✓' if no_json_detail else '✗'} 不再是裸 JSON")
ok_3 = is_html and shows_friendly and no_json_detail
print(f"  → {'PASS' if ok_3 else 'FAIL'}")

# ─── 测试 4：候选人页（真实 session）能正常开 ───
print("\n" + "=" * 70)
print("  测试 4: 真实 session 的候选人页（已完成的会显示结束页）")
print("=" * 70)
# 用测试 1 创建的那个 session
sid = data.get("session_id")  # 用最新的
code, ctype, body = http_get(f"/candidate/{sid}", accept="text/html")
print(f"  HTTP {code}  Content-Type: {ctype}  size: {len(body)}B")
text = body.decode("utf-8", errors="replace")
print(f"  → {'PASS' if code == 200 and '测评' in text else 'FAIL'}")

# ─── 总结 ───
print("\n" + "=" * 70)
all_ok = ok_1 and ok_2 and ok_3
print(f"  总体: {'ALL PASSED ✓' if all_ok else 'SOME FAILED ✗'}")
print("=" * 70)
print()
print("  修复点：")
print("  • 链接 host 现在自动跟随请求来源（不再硬编 ms.show）")
print("  • 404 走 HTML，手机浏览器看到友好提示页")
print("  • 支持 X-Forwarded-Proto/Host（魔搭反代场景）")
