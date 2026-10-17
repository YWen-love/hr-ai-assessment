"""后台启动 uvicorn 跑 hr-ai-assessment。日志写到 _server.log。"""
import os
import sys

PROJECT = r"C:\Users\15219\AI项目\hr-ai-assessment"
os.chdir(PROJECT)
sys.path.insert(0, PROJECT)
os.environ["GRADIO_SERVER_PORT"] = "7860"

# lifespan 中调 AI 的步骤做容错，避免没 API key 时起不来
from app.assessment_service import AssessmentService
_original = AssessmentService.prepare_job_knowledge
async def _safe(self):
    try:
        await _original(self)
    except Exception as e:
        print(f"[warn] prepare_job_knowledge skipped: {type(e).__name__}: {e}", flush=True)
AssessmentService.prepare_job_knowledge = _safe

import uvicorn
from main import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860, log_level="info")
