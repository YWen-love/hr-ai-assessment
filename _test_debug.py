"""测新加的 /debug/ai-status + 改进的错误信息。"""
import json
import urllib.request, urllib.parse, urllib.error

BASE = "http://127.0.0.1:7860"

def get(path, params=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read())
        except: return e.code, {"raw": e.read().decode("utf-8", errors="replace")[:500]}

print("=" * 70)
print("  测 /debug/ai-status  端点")
print("=" * 70)
print()
print("  本地（无 .env）应看到：")
print("    - api_key.is_set = False")
print("    - config_issues 列出缺失字段")
print("    - test_call.ok = False, error 给出明确提示")
print()

# 不实际调 AI，只看配置
code, data = get("/debug/ai-status", {"test": "0"})
print(f"  HTTP {code}")
print(json.dumps(data, ensure_ascii=False, indent=2))
print()

print("=" * 70)
print("  测 /debug/ai-status?test=1 实际调一次")
print("=" * 70)
code, data = get("/debug/ai-status")
print(f"  HTTP {code}")
print(json.dumps(data, ensure_ascii=False, indent=2))
