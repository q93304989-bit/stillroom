"""探针 v3：同一张图按三种尺寸发送，对比耗时、token 与描述质量。"""
import base64, io, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx
from PIL import Image
from app.config.credentials import load_env

PROMPT = ("只输出 JSON，不要解释。字段：subject(主体)、style(风格)、composition(构图)、"
          "lighting(光线)、flaws(瑕疵数组，可为空)。")


def main() -> int:
    env = load_env()
    key = env["DEEPSEEK_API_KEY"]
    base = (env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    model = "deepseek-v4-flash-vision-exp"

    media = Path(r"F:\AgnesGeneratorData\media")
    source = next(iter(sorted(media.glob("*.png"))), None) or next(iter(sorted(media.glob("*.jpg"))), None)
    if source is None:
        print("没有测试图片")
        return 1
    print(f"源图：{source.name}（{source.stat().st_size // 1024}KB）")

    variants: list[tuple[str, bytes]] = [("原图", source.read_bytes())]
    with Image.open(source) as im:
        for label, width in (("1024", 1024), ("320", 320)):
            ratio = width / max(im.size)
            resized = im.convert("RGB").resize((width, max(1, int(im.size[1] * ratio))), Image.LANCZOS)
            buffer = io.BytesIO()
            resized.save(buffer, "JPEG", quality=85)
            variants.append((label, buffer.getvalue()))

    for label, payload_bytes in variants:
        data_url = "data:image/jpeg;base64," + base64.b64encode(payload_bytes).decode()
        body = {"model": model, "max_tokens": 3000,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}}]}]}
        started = time.perf_counter()
        try:
            response = httpx.post(f"{base}/chat/completions",
                                  headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                  json=body, timeout=180)
        except Exception as exc:
            print(f"\n{label}（{len(payload_bytes)//1024}KB）: 异常 {type(exc).__name__}: {exc}")
            continue
        cost = time.perf_counter() - started
        if response.status_code != 200:
            print(f"\n{label}（{len(payload_bytes)//1024}KB）: HTTP {response.status_code} {response.text[:160]}")
            continue
        data = response.json()
        content = (data["choices"][0]["message"].get("content") or "").strip()
        usage = data.get("usage", {})
        reasoning = usage.get("completion_tokens_details", {}).get("reasoning_tokens")
        print(f"\n=== {label}（{len(payload_bytes)//1024}KB）耗时 {cost:.1f}s · "
              f"总 tokens={usage.get('total_tokens')}（reasoning={reasoning}）===")
        print((content[:420] if content else "（正文为空）").replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
