"""Translate ALL system_prompts_leaks files from English to Chinese using DeepSeek V4 Flash."""
import asyncio, aiohttp, os, sys, time

# Fix Windows console encoding
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 2026-09-15：原先硬编码了真实 Key（已进 git 历史，必须吊销轮换）。
# 改为从环境变量读取，密钥不再进版本库：
#   PowerShell:  $env:DEEPSEEK_KEY="sk-..."; python scripts/translate_fable5.py
API_KEY = os.environ.get("DEEPSEEK_KEY", "")
if not API_KEY:
    raise SystemExit("缺少 DEEPSEEK_KEY 环境变量——本脚本会真实调用 API，请先设置后再运行")
BASE_URL = "https://api.deepseek.com/v1"
MODEL = "deepseek-v4-flash"

SRC = r"D:\Yuncat\.trae\resources\system_prompts_leaks"
DST = r"D:\Yuncat\.trae\resources\system_prompts_leaks_zh"
CHUNK_SIZE = 2000

def collect_files(root):
    files = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(('.md', '.txt')) and not fn.startswith('.'):
                path = os.path.join(dirpath, fn)
                if os.path.getsize(path) > 30:
                    files.append(path)
    return sorted(files)

def split_chunks(text):
    paragraphs = text.split("\n\n")
    chunks, cur = [], ""
    for p in paragraphs:
        if len(cur) + len(p) < CHUNK_SIZE:
            cur = cur + ("\n\n" if cur else "") + p
        else:
            if cur.strip(): chunks.append(cur)
            cur = p
    if cur.strip(): chunks.append(cur)
    return chunks

async def translate(text, session):
    prompt = ("你是技术文档翻译。将以下英文翻译成中文。XML标签/URL/代码块保留不译。产品名保留英文。保持原文段落。只输出翻译:\n\n" + text)
    async with session.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={"model": MODEL, "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.0, "max_tokens": 4096},
        timeout=aiohttp.ClientTimeout(total=180),
    ) as resp:
        data = await resp.json()
        if "choices" in data:
            return data["choices"][0]["message"]["content"]
        raise RuntimeError(str(data))

async def process_file(session, src_path, total):
    rel = os.path.relpath(src_path, SRC)
    dst_path = os.path.join(DST, rel)

    with open(src_path, encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        return

    chunks = split_chunks(text)
    print(f"  {len(chunks)} chunks, {len(text)} chars -> ", end="", flush=True)

    results = []
    for i, chunk in enumerate(chunks):
        try:
            results.append(await translate(chunk, session))
            print(".", end="", flush=True)
        except Exception as e:
            print("X", end="", flush=True)
            results.append(chunk)
        await asyncio.sleep(0.2)

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(results))
    print(" OK")

async def main():
    files = collect_files(SRC)
    total = len(files)
    print(f"Model: {MODEL} | {total} files\n")

    connector = aiohttp.TCPConnector(limit=4)
    async with aiohttp.ClientSession(connector=connector) as session:
        for i, f in enumerate(files):
            print(f"[{i+1}/{total}] {os.path.relpath(f, SRC)[:90]}")
            try:
                await process_file(session, f, total)
            except Exception as e:
                print(f"  ERROR: {e}")

    # Count translated
    translated = sum(1 for _, _, files in os.walk(DST) for f in files)
    print(f"\nDone. {total} files processed, {translated} files output.")

if __name__ == "__main__":
    asyncio.run(main())
