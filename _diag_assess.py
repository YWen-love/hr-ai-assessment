"""诊断：测评端 3 步 AI 调用到底哪步炸的。"""
import json
import urllib.request, urllib.parse, urllib.error
import io, time, mimetypes
from pathlib import Path

BASE = "http://127.0.0.1:7860"
SAMPLE = Path(r"C:\Users\15219\AI项目\resumes_demo\data\张三简历.pdf")

def http(method, path, data=None, files=None):
    if files:
        boundary = "----b" + str(int(time.time()))
        body = io.BytesIO()
        for field, (filename, content) in files.items():
            body.write(f"--{boundary}\r\n".encode())
            body.write(f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode())
            body.write(f"Content-Type: {mimetypes.guess_type(filename)[0] or 'application/octet-stream'}\r\n\r\n".encode())
            body.write(content if isinstance(content, bytes) else content.encode())
            body.write(b"\r\n")
        for k, v in (data or {}).items():
            body.write(f"--{boundary}\r\n".encode())
            body.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
            body.write(v.encode())
            body.write(b"\r\n")
        body.write(f"--{boundary}--\r\n".encode())
        h = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    elif data is not None:
        h = {"Content-Type": "application/x-www-form-urlencoded"}
        body = urllib.parse.urlencode(data).encode()
    else:
        h = {}
        body = None
    # body 可能是 BytesIO（multipart）或 bytes（form）或 None
    if body is None:
        data_arg = None
    elif isinstance(body, io.BytesIO):
        data_arg = body.getvalue()
    else:
        data_arg = body
    req = urllib.request.Request(f"{BASE}{path}", data=data_arg, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read())
        except: return e.code, {"raw": e.read().decode("utf-8", errors="replace")[:500]}

# 上传简历
print("=" * 70)
print("  第 0 步: HR 上传简历")
print("=" * 70)
code, data = http("POST", "/hr/parse-link",
                  data={"candidate_name": "测试", "job_name": "人力实习生"},
                  files={"file": (SAMPLE.name, SAMPLE.read_bytes())})
sid = data["session_id"]
print(f"  session_id = {sid}\n")

# 启动测评
print("=" * 70)
print("  第 1 步: candidate/start — initial_question（本地 prompt，不调 AI）")
print("=" * 70)
code, data = http("POST", "/candidate/start", data={"session_id": sid})
first_q = data.get("first_question", "")
print(f"  HTTP {code}")
print(f"  first_question: {first_q[:200]}")
is_fallback_1 = "抱歉" in first_q or "测评到这里就结束" in first_q
print(f"  是 fallback？{is_fallback_1}（期望 False）")
print()

# 答一句，触发 generate_question
print("=" * 70)
print("  第 2 步: assessment/next — generate_question（调 AI）")
print("=" * 70)
code, data = http("POST", "/assessment/next",
                  data={"session_id": sid,
                        "user_answer": "是的大四，明年6月毕业，倾向实习转正。"})
next_q = data.get("next_question", "")
print(f"  HTTP {code}")
print(f"  next_question: {next_q[:200]}")
is_fallback_2 = "抱歉" in next_q or "系统暂时没有生成" in next_q
print(f"  是 fallback？{is_fallback_2}（期望 False → True 表示 API 调不通）")
print()

# 再答一句
print("=" * 70)
print("  第 3 步: assessment/next 第二轮")
print("=" * 70)
code, data = http("POST", "/assessment/next",
                  data={"session_id": sid,
                        "user_answer": "可以，每周5天，能实习6个月。"})
next_q2 = data.get("next_question", "")
print(f"  HTTP {code}")
print(f"  next_question: {next_q2[:200]}")
is_fallback_3 = "抱歉" in next_q2 or "系统暂时没有生成" in next_q2
print(f"  是 fallback？{is_fallback_3}")
print()

# 手动结束
print("=" * 70)
print("  第 4 步: assessment/finish — summarize（调 AI）")
print("=" * 70)
code, data = http("POST", "/assessment/finish", data={"session_id": sid})
summary = data.get("conversation_summary", "") or data.get("final_result", {}).get("reason_summary", "")
print(f"  HTTP {code}")
print(f"  conversation_summary: {summary[:200]}")
is_fallback_4 = "模型调用失败" in summary or "本地演示摘要模式" in summary
print(f"  是 fallback？{is_fallback_4}")
print()

# 查 _server.log 看实际异常
print("=" * 70)
print("  服务器日志最近 30 行（看真实异常）")
print("=" * 70)
import subprocess
log = Path(r"C:\Users\15219\AI项目\hr-ai-assessment\_server.log").read_text(encoding="utf-8", errors="replace")
# 找最近的异常块
lines = log.splitlines()
for i, line in enumerate(lines):
    if "DEEPSEEK" in line or "API" in line or "Error" in line or "error" in line or "Exception" in line or "Traceback" in line:
        start = max(0, i-1)
        end = min(len(lines), i+8)
        for j in range(start, end):
            print(f"    {lines[j]}")
        print("    ...")
print()
print("=" * 70)
print("  总结")
print("=" * 70)
print(f"  initial_question 工作：{not is_fallback_1}")
print(f"  generate_question 工作：{not is_fallback_2}")
print(f"  第 2 轮 generate_question 工作：{not is_fallback_3}")
print(f"  summarize 工作：{not is_fallback_4}")
print()
if is_fallback_2 or is_fallback_3 or is_fallback_4:
    print("  诊断结论：AI 调用没成功。原因看上面日志（最常见：DEEPSEEK_API_KEY 没填或填错）。")
    print("  解决：1) 在 .env 填入真实 key，重启；2) 或在魔搭 Space 平台设环境变量。")
