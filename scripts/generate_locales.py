"""Deep-merge en.json fallback into ja.json and ko.json.
已有 key 保留，缺失 key 用英文 fallback——消除 UI 上的 raw key 显示。
"""
import json, sys
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parent.parent / "src-frontend" / "src" / "i18n" / "locales"

def deep_merge(base: dict, existing: dict) -> dict:
    """深层合并——existing 优先，缺失的用 base 填充。"""
    result = {}
    for key in base:
        if key in existing:
            if isinstance(base[key], dict) and isinstance(existing[key], dict):
                result[key] = deep_merge(base[key], existing[key])
            else:
                result[key] = existing[key]  # 保留已有翻译
        else:
            result[key] = base[key]  # fallback 到英文
    # 保留 existing 中有但 base 中没有的 key（可能已废弃）
    for key in existing:
        if key not in result:
            result[key] = existing[key]
    return result

def main():
    en = json.loads(Path(LOCALE_DIR / "en.json").read_text(encoding="utf-8"))
    
    for lang in ["ja", "ko"]:
        path = LOCALE_DIR / f"{lang}.json"
        existing = json.loads(path.read_text(encoding="utf-8"))
        merged = deep_merge(en, existing)
        path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        en_keys = sum(1 for _ in _count_keys(en))
        merged_keys = sum(1 for _ in _count_keys(merged))
        print(f"{lang}.json: {merged_keys}/{en_keys} keys (was {sum(1 for _ in _count_keys(existing))})")

def _count_keys(d, prefix=""):
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            yield from _count_keys(v, key)
        else:
            yield key

if __name__ == "__main__":
    main()
