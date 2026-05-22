#!/usr/bin/env python3
import os
import re
import json
import base64
import time
import datetime
import requests
from pathlib import Path

DATA_URL = "https://x.deepsrt.cc/index.txt"
GOOGLE_API_KEY = os.environ["GEMINI_API_KEY"]
TTS_URL = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_API_KEY}"
TTS_VOICE = "cmn-TW-Wavenet-A"
DOCS_DIR = Path("docs")
AUDIO_DIR = DOCS_DIR / "audio"
MANIFEST_PATH = DOCS_DIR / "manifest.json"
MAX_DAYS = 10


def fetch_index():
    resp = requests.get(DATA_URL, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_index(text):
    date_match = re.search(r'台北時間\s+(\d{4}-\d{2}-\d{2})', text)
    date = date_match.group(1) if date_match else datetime.date.today().isoformat()

    categories = []
    current_cat = None
    global_index = 0

    for line in text.splitlines():
        line = line.strip()
        if line.startswith('###'):
            if current_cat:
                categories.append(current_cat)
            current_cat = {'name': line.lstrip('#').strip(), 'items': []}
        elif line.startswith('- ') and current_cat:
            raw = line[2:].strip()
            tweet_match = re.search(r'（(\d+)）\s*$', raw)
            tweet_id = tweet_match.group(1) if tweet_match else ''
            clean = re.sub(r'（\d+）\s*$', '', raw).strip()
            global_index += 1
            current_cat['items'].append({
                'index': global_index,
                'text': clean,
                'tweet_id': tweet_id,
            })

    if current_cat:
        categories.append(current_cat)

    return date, categories


def text_to_mp3(text, output_path, max_retries=5):
    payload = {
        "input": {"text": text},
        "voice": {
            "languageCode": "cmn-TW",
            "name": TTS_VOICE,
        },
        "audioConfig": {
            "audioEncoding": "MP3",
            "speakingRate": 1.0,
        },
    }
    for attempt in range(max_retries):
        resp = requests.post(TTS_URL, json=payload, timeout=30)
        if resp.status_code == 429 and attempt < max_retries - 1:
            wait = int(resp.headers.get('Retry-After', 30))
            print(f"  rate limit, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        output_path.write_bytes(base64.b64decode(resp.json()["audioContent"]))
        return


def load_manifest():
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
    return {'updated_at': None, 'dates': []}


def save_manifest(manifest):
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def cleanup_old_dates(manifest):
    dates = manifest.get('dates', [])
    if len(dates) <= MAX_DAYS:
        return manifest
    dates.sort(key=lambda x: x['date'], reverse=True)
    for entry in dates[MAX_DAYS:]:
        old_dir = AUDIO_DIR / entry['date']
        if old_dir.exists():
            for f in old_dir.iterdir():
                f.unlink()
            old_dir.rmdir()
    manifest['dates'] = dates[:MAX_DAYS]
    return manifest


def main():
    raw = fetch_index()
    date, categories = parse_index(raw)
    total_expected = sum(len(cat['items']) for cat in categories)
    print(f"Date: {date}, categories: {len(categories)}, expected: {total_expected}")

    manifest = load_manifest()

    # 找到已有的当天记录，提取已成功生成的文件集合（file 非 null）
    existing_entry = next((d for d in manifest.get('dates', []) if d['date'] == date), None)
    existing_files = set()
    if existing_entry:
        for cat in existing_entry.get('categories', []):
            for item in cat.get('items', []):
                if item.get('file'):
                    existing_files.add(item['file'])
        already_done = len(existing_files)
        if existing_entry.get('complete') and already_done >= total_expected:
            print(f"{date} already complete ({already_done}/{total_expected}), skipping.")
            return
        print(f"{date} resuming: {already_done}/{total_expected} already done.")

    # 移除旧记录，重新构建（保留已有 MP3 文件）
    manifest['dates'] = [d for d in manifest.get('dates', []) if d['date'] != date]

    date_dir = AUDIO_DIR / date
    date_dir.mkdir(parents=True, exist_ok=True)

    tz_cst = datetime.timezone(datetime.timedelta(hours=8))
    date_entry = {
        'date': date,
        'generated_at': datetime.datetime.now(tz_cst).isoformat(),
        'categories': [],
    }

    success = 0
    for cat in categories:
        cat_entry = {'name': cat['name'], 'items': []}
        for item in cat['items']:
            idx = item['index']
            filename = f"{idx:03d}.mp3"
            output_path = date_dir / filename
            file_key = f"audio/{date}/{filename}"

            # 已有 MP3 文件则直接复用，不重新生成
            if output_path.exists() and file_key in existing_files:
                cat_entry['items'].append({
                    'index': idx,
                    'text': item['text'],
                    'tweet_id': item['tweet_id'],
                    'file': file_key,
                })
                success += 1
                continue

            print(f"  [{idx:03d}] {item['text'][:60]}")
            try:
                text_to_mp3(item['text'], output_path)
                cat_entry['items'].append({
                    'index': idx,
                    'text': item['text'],
                    'tweet_id': item['tweet_id'],
                    'file': file_key,
                })
                success += 1
            except Exception as e:
                print(f"  ERROR [{idx:03d}]: {e}")
                # 失败的条目 file 设为 null，仍写入 manifest 供前端显示
                cat_entry['items'].append({
                    'index': idx,
                    'text': item['text'],
                    'tweet_id': item['tweet_id'],
                    'file': None,
                })
            time.sleep(0.2)

        date_entry['categories'].append(cat_entry)

    # 只有全部成功才标记 complete
    if success == total_expected:
        date_entry['complete'] = True
        print(f"Done: {success}/{total_expected} ✓")
    else:
        print(f"Partial: {success}/{total_expected} — will resume on next run")

    manifest.setdefault('dates', []).insert(0, date_entry)
    manifest = cleanup_old_dates(manifest)
    manifest['updated_at'] = datetime.datetime.now(tz_cst).isoformat()
    save_manifest(manifest)


if __name__ == '__main__':
    main()
