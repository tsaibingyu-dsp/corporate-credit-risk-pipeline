
import os
import time
from google import genai
from google.genai import types

client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options=types.HttpOptions(
        timeout=120000,
        retry_options=types.HttpRetryOptions(attempts=1)
    )
)

model = os.environ["GEMINI_MODEL"]

tests = [
    (
        "測試1：短文字",
        "請回答：測試成功",
        None
    ),
    (
        "測試2：簡單JSON",
        "請將公司名稱台積電與風險類別無轉成JSON",
        {
            "response_mime_type": "application/json",
            "response_json_schema": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "risk": {"type": "string"}
                },
                "required": ["company", "risk"]
            }
        }
    )
]

for name, prompt, config in tests:
    print(f"\n{name}", flush=True)
    start = time.monotonic()

    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=config
        )
        print("成功：", response.text[:500])
    except Exception as e:
        print("失敗：", type(e).__name__)
        print("HTTP：", getattr(e, "code", None))
        print("錯誤：", str(e)[:500])

    print(
        "耗時：",
        round(time.monotonic() - start, 2),
        "秒"
    )
