#!/usr/bin/env python3
import os
import re
import json
import time
import datetime
import requests
import lameenc
from pathlib import Path
from google import genai
from google.genai import types

DATA_URL = "https://x.deepsrt.cc/index.txt"
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
DOCS_DIR = Path("docs")
AUDIO_DIR = DOCS_DIR / "audio"
MANIFEST_PATH = DOCS_DIR / "manifest.json"
MAX_DAYS = 10
TTS_MODEL = "gemini-2.5-flash-preview-tts"
TTS_VOICE = "Aoede"


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


def pcm_to_mp3(pcm_bytes, output_path):
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(96)
    encoder.set_in_sample_rate(24000)
    encoder.set_channels(1)
    encoder.set_quality(7)
    mp3_data = encoder.encode(pcm_bytes)
    mp3_data += encoder.flush()
    output_path.write_bytes(mp3_data)


def text_to_mp3(client, text, output_path, max_retries=5):
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=TTS_MODEL,
                contents=text,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=TTS_VOICE
                            )
                        )
                    ),
                ),
            )
            audio_bytes = response.candidates[0].content.parts[0].inline_data.data
            pcm_to_mp3(audio_bytes, output_path)
            return
        except Exception as e:
            msg = str(e)
            if '429' in msg and attempt < max_retries - 1:
                # 从错误信息提取建议等待时间，默认 65 秒
                import re as _re
                m = _re.search(r'retry[^\d]*(\d+)s', msg, _re.IGNORECASE)
                wait = int(m.group(1)) + 3 if m else 65
                print(f"  rate limit, waiting {wait}s...")
                time.sleep(wait)
            else:
                raise


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
    print(f"Date: {date}, categories: {len(categories)}")

    manifest = load_manifest()
    # 只跳过已完整生成的日期
    for d in manifest.get('dates', []):
        if d['date'] == date and d.get('complete'):
            print(f"{date} already complete, skipping.")
            return
    # 移除当天不完整的旧记录
    manifest['dates'] = [d for d in manifest.get('dates', []) if d['date'] != date]

    date_dir = AUDIO_DIR / date
    date_dir.mkdir(parents=True, exist_ok=True)

    client = genai.Client(api_key=GEMINI_API_KEY)
    tz_cst = datetime.timezone(datetime.timedelta(hours=8))

    date_entry = {
        'date': date,
        'generated_at': datetime.datetime.now(tz_cst).isoformat(),
        'categories': [],
    }

    for cat in categories:
        cat_entry = {'name': cat['name'], 'items': []}
        for item in cat['items']:
            idx = item['index']
            filename = f"{idx:03d}.mp3"
            output_path = date_dir / filename
            print(f"  [{idx:03d}] {item['text'][:60]}")
            try:
                text_to_mp3(client, item['text'], output_path)
                cat_entry['items'].append({
                    'index': idx,
                    'text': item['text'],
                    'tweet_id': item['tweet_id'],
                    'file': f"audio/{date}/{filename}",
                })
            except Exception as e:
                print(f"  ERROR [{idx:03d}]: {e}")
            time.sleep(6)  # gemini-2.5-flash-preview-tts: 10 RPM hard limit
        date_entry['categories'].append(cat_entry)

    date_entry['complete'] = True
    manifest.setdefault('dates', []).insert(0, date_entry)
    manifest = cleanup_old_dates(manifest)
    manifest['updated_at'] = datetime.datetime.now(tz_cst).isoformat()
    save_manifest(manifest)

    total = sum(len(c['items']) for c in date_entry['categories'])
    print(f"Done: {total} MP3s for {date}")


if __name__ == '__main__':
    main()
