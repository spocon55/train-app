"""
Web用データ生成スクリプト
- aobadai_timetable.json（各停のみ）
- ODPT 有楽町線・永田町 時刻表
を統合して web/data.json を生成する。

実行: python build_web_data.py
"""

import json
import os
import subprocess
import sys
import io
from datetime import datetime
from pathlib import Path
import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

API_KEY  = os.environ.get("ODPT_API_KEY",
           "thpqo0oushhtq3wzf8k80t5x8e7s7uhg7m2gdkat2emdfjzztasse0cgmxyv24y3")
BASE_URL = "https://api.odpt.org/api/v4/"

NAGATACHO_TO_IIDABASHI_FALLBACK = 6  # 列車番号マッチング失敗時のフォールバック（分）


def to_min(t: str) -> int:
    h, m = map(int, t.split(":"))
    return (h + 24 if h < 5 else h) * 60 + m


def from_min(total: int) -> str:
    h, m = divmod(total % (24 * 60), 60)
    return f"{h:02d}:{m:02d}"

DATA_DIR      = Path(__file__).parent
AOBADAI_FILE  = DATA_DIR / "aobadai_timetable.json"
OUTPUT_FILE   = DATA_DIR / "docs" / "data.json"


def load_aobadai() -> dict:
    """aobadai_timetable.json から各停のみ抽出して返す"""
    if not AOBADAI_FILE.exists():
        raise FileNotFoundError(
            f"{AOBADAI_FILE} が見つかりません。\n"
            "先に scrape_timetable.py を実行してください。"
        )
    with open(AOBADAI_FILE, encoding="utf-8") as f:
        data = json.load(f)

    result = {}
    for day_key, trains in data["timetable"].items():
        result[day_key] = [t for t in trains if t["type_ja"] == "各停"]
    return result


def fetch_odpt(same_as: str) -> list[dict]:
    """ODPT StationTimetable を取得して stationTimetableObject を返す"""
    params = {"acl:consumerKey": API_KEY, "owl:sameAs": same_as}
    res = requests.get(BASE_URL + "odpt:StationTimetable", params=params, timeout=15)
    if res.status_code == 200 and res.json():
        return res.json()[0].get("odpt:stationTimetableObject", [])
    return []


def fetch_yurakucho(calendar: str) -> list[dict]:
    """有楽町線・永田町（新木場方向）の時刻表を取得し、飯田橋着時刻をマッチングして付与する。
    マッチング優先度:
      1. odpt:trainNumber が一致する飯田橋エントリの発時刻
      2. 永田町発時刻 +4〜+9分 の時刻窓内で最初に見つかる飯田橋発時刻
    """
    nagatacho_raw = fetch_odpt(
        f"odpt.StationTimetable:TokyoMetro.Yurakucho.Nagatacho.TokyoMetro.ShinKiba.{calendar}"
    )
    iidabashi_raw = fetch_odpt(
        f"odpt.StationTimetable:TokyoMetro.Yurakucho.Iidabashi.TokyoMetro.ShinKiba.{calendar}"
    )

    # 飯田橋: 列車番号 → 発時刻 の辞書
    iidabashi_by_num = {
        e["odpt:trainNumber"]: e["odpt:departureTime"]
        for e in iidabashi_raw if e.get("odpt:trainNumber")
    }
    # 飯田橋: 分単位リスト（時刻窓フォールバック用）
    iidabashi_mins = [to_min(e["odpt:departureTime"]) for e in iidabashi_raw]

    matched_count = fallback_count = null_count = 0
    result = []
    for e in nagatacho_raw:
        dep = e["odpt:departureTime"]
        dep_min = to_min(dep)
        train_num = e.get("odpt:trainNumber")

        # 第1優先: 列車番号マッチング
        if train_num and train_num in iidabashi_by_num:
            iidabashi_dep = iidabashi_by_num[train_num]
            matched_count += 1
        else:
            # フォールバック: 時刻窓 +4〜+9分
            matched_min = next(
                (t for t in iidabashi_mins if dep_min + 4 <= t <= dep_min + 9),
                None
            )
            if matched_min is not None:
                iidabashi_dep = from_min(matched_min)
                fallback_count += 1
            else:
                iidabashi_dep = None
                null_count += 1

        result.append({"time": dep, "iidabashi_dep": iidabashi_dep})

    print(f"     列車番号マッチ: {matched_count} 本 / 時刻窓フォールバック: {fallback_count} 本 / 未マッチ: {null_count} 本")
    return result


def main():
    print("=" * 50)
    print("  Web用データ生成")
    print(f"  実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    # 青葉台時刻表
    print("\n📂 青葉台時刻表 読込中...")
    aobadai = load_aobadai()
    for day, trains in aobadai.items():
        print(f"   {day}: 各停 {len(trains)} 本")

    # 有楽町線・永田町 時刻表（青葉台→飯田橋 用）
    print("\n📡 有楽町線・永田町（新木場方向） 取得中...")
    yk_weekday  = fetch_yurakucho("Weekday")
    yk_holiday  = fetch_yurakucho("SaturdayHoliday")
    print(f"   平日: {len(yk_weekday)} 本  土曜・休日: {len(yk_holiday)} 本")
    if not yk_weekday:
        print("  ⚠️  取得失敗。APIキーとネットワークを確認してください。")

    # 統合 JSON 生成
    output = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "aobadai": aobadai,
        "yurakucho_nagatacho": {          # 青葉台→飯田橋: 永田町→飯田橋
            "weekday":  yk_weekday,
            "saturday": yk_holiday,
            "holiday":  yk_holiday,
        },
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n✅ 保存完了: {OUTPUT_FILE}")
    print(f"   ファイルサイズ: {OUTPUT_FILE.stat().st_size // 1024} KB")

    # GitHub へ自動 push
    print("\n📤 GitHub へ push 中...")
    repo_dir = DATA_DIR
    try:
        subprocess.run(["git", "add", str(OUTPUT_FILE)], cwd=repo_dir, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"時刻表更新 {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            cwd=repo_dir, check=True
        )
        subprocess.run(["git", "push"], cwd=repo_dir, check=True)
        print("✅ GitHub Pages に反映しました（反映まで数分かかる場合があります）")
    except subprocess.CalledProcessError as e:
        print(f"⚠️  git push に失敗しました: {e}")
        print("   手動で git add / commit / push を実行してください。")


if __name__ == "__main__":
    main()
