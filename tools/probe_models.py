"""模型探针 v2：修正 max_tokens 与 Choice 格式，并对比「原图 vs 缩略图」的耗时。"""
import base64, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx
from app.config.credentials import load_env

PROMPT = ("只输出 JSON，不要解释。字段：subject(主体)、style(风格)、composition(构图)、"
          "lighting(光线)、flaws(瑕疵数组，可为空)。")


def call_vision(base, key, model, image_path, max_tokens=3000):
    data_url = "data:image/jpeg;base64," + base64.b64encode(image_path.read_bytes()).decode()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
        "max_tokens": max_tokens,
    }
    started = time.perf_counter()
    response = httpx.post(f"{base}/chat/completions",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json=payload, timeout=180)
    cost = time.perf_counter() - started
    return response, cost


def main() -> int:
    env = load_env()
    media = Path(r"F:\AgnesGeneratorData\media")
    image = next(iter(sorted(media.glob("*.png"))), None)
    if image is None:
        print("没有找到测试图片")
        return 1
    thumb = Path(r"F:\AgnesGeneratorData\thumbs") / (image.stem + ".jpg")

    key = env["DEEPSEEK_API_KEY"]
    base = (env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    model = "deepseek-v4-flash-vision-exp"

    for label, path in (("原图", image), ("缩略图", thumb)):
        if not path.is_file():
            print(f"{label}: 文件不存在，跳过")
            continue
        size_kb = path.stat().st_size // 1024
        try:
            response, cost = call_vision(base, key, model, path)
        except Exception as exc:
            print(f"{label}（{size_kb}KB）: 请求异常 {type(exc).__name__}: {exc}")
            continue
        if response.status_code != 200:
            print(f"{label}（{size_kb}KB）: HTTP {response.status_code} {response.text[:200]}")
            continue
        body = response.json()
        message = body["choices"][0]["message"]
        content = (message.get("content") or "").strip()
        usage = body.get("usage", {})
        print(f"\n=== {label}（{size_kb}KB）耗时 {cost:.1f}s · tokens={usage.get('total_tokens')} "
              f"(reasoning={usage.get('completion_tokens_details', {}).get('reasoning_tokens')}) ===")
        print((content[:700] if content else "（正文为空）").replace("\n", " "))

    # ---------------- Jev（Choice criteria 改字典）----------------
    print("\n=== Jev ===")
    ts_key = env.get("TYPESAFE_API_KEY", "")
    payload = {
        "model": "jev-latest",
        "state": {"requirement": "中秋节海报，竖版，国潮风格",
                  "image_description": "一轮明月下的中式庭院，暖色灯笼，竖构图，柔和侧光，无明显瑕疵"},
        "questions": {
            "fits": {"type": "noul", "instructions": "这张图是否符合用户需求（中秋主题 + 竖版 + 国潮风格）"},
            "quality": {"type": "score", "instructions": "作为海报的可用程度",
                        "criteria": ["完全不可用", "需要大改", "小修即可", "直接可用"]},
            "fix": {"type": "choice", "instructions": "最该改进的地方",
                    "criteria": {"none": "不需要改", "subject": "主体不对或太弱",
                                 "style": "风格不符", "composition": "构图问题",
                                 "lighting": "光线问题"}},
        },
    }
    started = time.perf_counter()
    response = httpx.post("https://api.typesafe.ai/v1/systemone",
                          headers={"Authorization": f"Bearer {ts_key}", "Content-Type": "application/json"},
                          json=payload, timeout=60)
    cost = time.perf_counter() - started
    print(f"HTTP {response.status_code} · 耗时 {cost:.1f}s")
    if response.status_code == 200:
        print(json.dumps(response.json(), ensure_ascii=False, indent=1)[:1200])
    else:
        print(response.text[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
