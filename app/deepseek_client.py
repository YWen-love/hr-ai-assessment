import httpx

from config import settings


class DeepSeekClient:
    def __init__(self):
        self.api_key = settings.DEEPSEEK_API_KEY
        self.base_url = settings.DEEPSEEK_BASE_URL.rstrip("/")
        self.model = settings.DEEPSEEK_MODEL

    async def chat(self, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            raise ValueError("DEEPSEEK_API_KEY is empty. Fill in .env first.")

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, headers=headers, json=payload)

        if response.status_code != 200:
            raise RuntimeError(f"DeepSeek API error: {response.text}")

        data = response.json()
        return data["choices"][0]["message"]["content"]
