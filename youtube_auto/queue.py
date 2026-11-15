"""CSV / JSON 업로드 목록 읽기 및 업로드 기록(중복 방지)."""
import csv
import json
from pathlib import Path


def load_items(path):
    p = Path(path)
    if p.suffix.lower() == ".json":
        return json.loads(p.read_text(encoding="utf-8"))
    with p.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_log(path="upload_log.json"):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_log(log, path="upload_log.json"):
    Path(path).write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
