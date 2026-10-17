"""模拟浏览器走一遍 hr-ai-assessment 各页面。"""
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import io
import json
import mimetypes
from pathlib import Path

BASE = "http://127.0.0.1:7860"

def http(method, url, data=None, files=None, headers=None, timeout=10):
    if files:
        boundary = "----b" + str(int(time.time()))
        body = io.BytesIO()
        for field, (filename, content) in files.items():
            body.write(f"--{boundary}\r\n".encode())
            body.write(f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode())
            ct = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            body.write(f"Content-Type: {ct}\r\n\r\n".encode())
            body.write(content if isinstance(content, bytes) else content.encode())
            body.write(b"\r\n")
        for k, v in (data or {}).items():
            body.write(f"--{boundary}\r\n".encode())
            body.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
            body.write(v.encode())
            body.write(b"\r\n")
        body.write(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(url, data=body.getvalue(), method=method, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            **(headers or {}),
        })
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            **(headers or {}),
        })
    else:
        req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

def section(title):
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)

def show_html(text, max_chars=600):
    text = text.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        print(text[:max_chars])
        print(f"  ... [共 {len(text)} 字符，已截断]")
    else:
        print(text)

def show_json(text, max_chars=500):
    text = text.decode("utf-8", errors="replace")
    try:
        obj = json.loads(text)
        s = json.dumps(obj, ensure_ascii=False, indent=2)
        if len(s) > max_chars:
            print(s[:max_chars] + f"\n  ... [共 {len(text)} 字符，已截断]")
        else:
            print(s)
    except Exception:
        print(text[:max_chars])

# ─────────────────────────────────────────────────────────────
# 1. 根路径
# ─────────────────────────────────────────────────────────────
section("1. GET  /  （API 根路径，无 session/admin 时返回基础信息）")
code, hdr, body = http("GET", f"{BASE}/")
print(f"  HTTP {code}  Content-Type: {hdr.get('Content-Type', '?')}  大小: {len(body)}B")
show_json(body)

# ─────────────────────────────────────────────────────────────
# 2. Gradio HR 工作台
# ─────────────────────────────────────────────────────────────
section("2. GET  /hr  （Gradio HR 工作台页面）")
code, hdr, body = http("GET", f"{BASE}/hr")
print(f"  HTTP {code}  Content-Type: {hdr.get('Content-Type', '?')}  大小: {len(body)}B")
text = body.decode("utf-8", errors="replace")
# 抓关键标识
import re
m = re.search(r'<title>([^<]+)</title>', text)
if m: print(f"  <title>: {m.group(1)}")
m = re.search(r'简历评测 · HR工作台', text)
if m: print(f"  含工作台标题: ✓")
m = re.search(r'候选人筛选列表', text)
if m: print(f"  含候选人列表区: ✓")
m = re.search(r'生成候选人链接', text)
if m: print(f"  含生成按钮: ✓")
m = re.search(r'查看测评结果', text)
if m: print(f"  含结果按钮: ✓")

# ─────────────────────────────────────────────────────────────
# 3. HR 上传简历生成链接
# ─────────────────────────────────────────────────────────────
section("3. POST /hr/parse-link  （HR 上传真实简历生成候选人链接）")
sample_pdf = Path(r"C:\Users\15219\AI项目\resumes_demo\data\张三简历.pdf")
if not sample_pdf.exists():
    sample_pdf = Path(r"C:\Users\15219\AI项目\resumes_demo\data\易小文cpjl.pdf")
print(f"  简历文件: {sample_pdf.name}  ({sample_pdf.stat().st_size} bytes)")
code, hdr, body = http(
    "POST", f"{BASE}/hr/parse-link",
    data={"candidate_name": "张三", "job_name": "人力实习生"},
    files={"file": (sample_pdf.name, sample_pdf.read_bytes())},
)
print(f"  HTTP {code}  大小: {len(body)}B")
show_json(body)
parsed = json.loads(body)
session_id = parsed.get("session_id")
public_link = parsed.get("public_link")
print(f"\n  ★ session_id  = {session_id}")
print(f"  ★ public_link = {public_link}")

# ─────────────────────────────────────────────────────────────
# 4. HR 会话列表
# ─────────────────────────────────────────────────────────────
section("4. GET  /hr/sessions  （HR 候选人筛选列表）")
code, hdr, body = http("GET", f"{BASE}/hr/sessions")
print(f"  HTTP {code}  大小: {len(body)}B")
show_json(body)

# ─────────────────────────────────────────────────────────────
# 5. 候选人测评页（HTML 页面）
# ─────────────────────────────────────────────────────────────
section(f"5. GET  /candidate/{session_id}  （候选人对话页面）")
code, hdr, body = http("GET", f"{BASE}/candidate/{session_id}")
print(f"  HTTP {code}  Content-Type: {hdr.get('Content-Type', '?')}  大小: {len(body)}B")
text = body.decode("utf-8", errors="replace")
m = re.search(r'<title>([^<]+)</title>', text)
if m: print(f"  <title>: {m.group(1)}")
m = re.search(r"sessionId = '([^']+)'", text)
if m: print(f"  注入的 sessionId: {m.group(1)}  (匹配: {m.group(1) == session_id})")
m = re.search(r'请输入你的回答', text)
if m: print(f"  含输入框占位符: ✓")

# ─────────────────────────────────────────────────────────────
# 6. 启动测评会话
# ─────────────────────────────────────────────────────────────
section("6. POST /candidate/start  （候选人进入测评，AI 提第 1 个问题）")
code, hdr, body = http("POST", f"{BASE}/candidate/start", data={"session_id": session_id})
print(f"  HTTP {code}  大小: {len(body)}B")
try:
    j = json.loads(body)
    print(f"  候选人: {j.get('candidate_name')}")
    print(f"  岗位:   {j.get('job_name')}")
    print(f"  首问:   {(j.get('first_question') or '<<None - AI 未启动>>')[:200]}")
    if not j.get("first_question"):
        print(f"  ⚠️ first_question 为空，AI 端点没回（DEEPSEEK_API_KEY 没填）")
        print(f"  完整响应: {json.dumps(j, ensure_ascii=False, indent=2)[:400]}")
except Exception as e:
    show_json(body)

# ─────────────────────────────────────────────────────────────
# 7. 候选人回答（如果 AI 可用，会继续提问；不可用就走降级路径）
# ─────────────────────────────────────────────────────────────
section("7. POST /assessment/next  （候选人回答后获取下一题）")
code, hdr, body = http("POST", f"{BASE}/assessment/next",
                       data={"session_id": session_id, "user_answer": "我目前大四，明年6月毕业，实习过3个月的人事助理岗位。"})
print(f"  HTTP {code}  大小: {len(body)}B")
try:
    j = json.loads(body)
    print(f"  已收到答案: {j.get('answer_received')}")
    print(f"  下一题:     {(j.get('next_question') or '<<None>>')[:200]}")
    print(f"  状态:       {j.get('status')}")
except Exception:
    show_json(body)

# ─────────────────────────────────────────────────────────────
# 8. 手动标记完成，看 HR 端结果
# ─────────────────────────────────────────────────────────────
section("8. POST /assessment/finish  （HR 手动结束会话）")
code, hdr, body = http("POST", f"{BASE}/assessment/finish", data={"session_id": session_id})
print(f"  HTTP {code}  大小: {len(body)}B")
show_json(body)

# ─────────────────────────────────────────────────────────────
# 9. HR 端查看结果
# ─────────────────────────────────────────────────────────────
section("9. GET  /hr/result/{session_id}  （HR 查看测评结论）")
code, hdr, body = http("GET", f"{BASE}/hr/result/{session_id}")
print(f"  HTTP {code}  大小: {len(body)}B")
show_json(body)

# ─────────────────────────────────────────────────────────────
# 10. 候选人刷新（已完成态展示）
# ─────────────────────────────────────────────────────────────
section("10. GET /candidate/{session_id}  （候选人刷新 - 已完成态页面）")
code, hdr, body = http("GET", f"{BASE}/candidate/{session_id}")
print(f"  HTTP {code}  Content-Type: {hdr.get('Content-Type', '?')}  大小: {len(body)}B")
text = body.decode("utf-8", errors="replace")
m = re.search(r'<title>([^<]+)</title>', text)
if m: print(f"  <title>: {m.group(1)}")
m = re.search(r'测评已结束', text)
if m: print(f"  结束页文案: ✓")
m = re.search(r'感谢你的参与', text)
if m: print(f"  感谢语: ✓")

print()
print("=" * 70)
print("  浏览器可访问的链接汇总：")
print(f"    HR 工作台 (Gradio):  http://127.0.0.1:7860/hr")
print(f"    HR 上传页 (HTML):    http://127.0.0.1:7860/")
print(f"    候选人测评页:        {public_link}")
print(f"    HR 会话列表 API:     http://127.0.0.1:7860/hr/sessions")
print("=" * 70)
