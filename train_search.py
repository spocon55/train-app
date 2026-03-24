"""
青葉台 → 永田町 → 飯田橋  乗換案内アプリ v4
- 青葉台発: Playwright取得の正確な時刻表 (aobadai_timetable.json)
- 永田町着: 列車別の実際の通過時刻 (aobadai_timetable.json の nagatacho_arr)
- 永田町→飯田橋: ODPT API（東京メトロ南北線・有楽町線）
- 乗換バッファ: user_settings.json に保存・読込
"""

import json
import os
import sys
import io
from datetime import datetime
from pathlib import Path
from typing import Optional
import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ============================================================
# 設定
# ============================================================
API_KEY   = os.environ.get("ODPT_API_KEY",
            "thpqo0oushhtq3wzf8k80t5x8e7s7uhg7m2gdkat2emdfjzztasse0cgmxyv24y3")
BASE_URL  = "https://api.odpt.org/api/v4/"
DATA_DIR  = Path(__file__).parent
TIMETABLE_FILE  = DATA_DIR / "aobadai_timetable.json"
SETTINGS_FILE   = DATA_DIR / "user_settings.json"

# 永田町→飯田橋 フォールバック所要時間（分）- iidabashi_dep が無い場合のみ使用
NAGATACHO_TO_IIDABASHI_FALLBACK = 6

# nagatacho_arr が無い場合のフォールバック（渋谷止まり各停など）
AOBADAI_TO_NAGATACHO_APPROX = {"各停": 51}


# ============================================================
# ユーティリティ
# ============================================================
def to_min(time_str: str) -> int:
    """'HH:MM' → 分（深夜 00:xx〜04:xx は+24h）"""
    h, m = map(int, time_str.split(":"))
    total = h * 60 + m
    if h < 5:
        total += 24 * 60
    return total

def from_min(total: int) -> str:
    """分 → 'HH:MM'"""
    h = total // 60 % 24
    m = total % 60
    return f"{h:02d}:{m:02d}"


# ============================================================
# ユーザー設定（乗換バッファ）
# ============================================================
DEFAULT_TRANSFER_BUFFER = 5

def load_settings() -> dict:
    """user_settings.json を読み込む（存在しなければデフォルトを返す）"""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"transfer_buffer_minutes": DEFAULT_TRANSFER_BUFFER}

def save_settings(settings: dict) -> None:
    """user_settings.json に保存"""
    SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def ask_transfer_buffer(current: int) -> int:
    """乗換バッファ設定を確認・変更する"""
    print(f"\n現在の設定: 永田町 乗換バッファ = {current} 分")
    ans = input("変更しますか？ [y/N]: ").strip().lower()
    if ans == "y":
        while True:
            try:
                val = int(input("新しい乗換バッファ（分）を入力: ").strip())
                if 0 <= val <= 30:
                    return val
                print("  0〜30 の整数で入力してください。")
            except ValueError:
                print("  整数で入力してください。")
    return current


# ============================================================
# データ読み込み
# ============================================================
def load_aobadai_timetable(day_type: str) -> list[dict]:
    """aobadai_timetable.json から指定ダイヤを読み込む"""
    if not TIMETABLE_FILE.exists():
        raise FileNotFoundError(
            f"{TIMETABLE_FILE} が見つかりません。\n"
            "先に scrape_timetable.py を実行してください。"
        )
    with open(TIMETABLE_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data["timetable"].get(day_type, data["timetable"]["weekday"])

def fetch_nagatacho_timetable(day_type: str) -> dict:
    """ODPT から永田町→飯田橋（有楽町線）の時刻表を取得"""
    cal = "Weekday" if day_type == "weekday" else "SaturdayHoliday"
    result = {}

    # 有楽町線（和光市方向 → 飯田橋は永田町より和光市寄り）
    params = {
        "acl:consumerKey": API_KEY,
        "owl:sameAs": f"odpt.StationTimetable:TokyoMetro.Yurakucho.Nagatacho.TokyoMetro.Wakoshi.{cal}",
    }
    res = requests.get(BASE_URL + "odpt:StationTimetable", params=params, timeout=15)
    if res.status_code == 200 and res.json():
        entries = res.json()[0].get("odpt:stationTimetableObject", [])
        result["yurakucho"] = [
            {"time": e["odpt:departureTime"],
             "dest": e.get("odpt:destinationStation", [""])[0].split(".")[-1]}
            for e in entries
        ]
    else:
        result["yurakucho"] = []

    return result


# ============================================================
# ルート構築
# ============================================================
def build_routes(aobadai_trains: list[dict], nagatacho_tt: dict,
                 transfer_buffer: int) -> list[dict]:
    """
    青葉台時刻表 × 永田町時刻表 → 乗継ルートを計算
    nagatacho_arr が JSON に保存されていればそれを使用、なければ近似値
    """
    routes = []

    for train in aobadai_trains:
        dep_time   = train["time"]
        train_type = train["type_ja"]
        dest       = train["dest"]
        dep_min    = to_min(dep_time)

        # 永田町到着時刻の決定
        if train.get("nagatacho_arr"):
            nagatacho_min = to_min(train["nagatacho_arr"])
            time_source = "実測"
        else:
            nagatacho_min = dep_min + AOBADAI_TO_NAGATACHO_APPROX.get(train_type, 51)
            time_source = "近似"

        # 永田町で最初に乗れる電車（乗換バッファ込み）
        best = None
        for line, entries in nagatacho_tt.items():
            for e in entries:
                t_min = to_min(e["time"])
                if t_min >= nagatacho_min + transfer_buffer:
                    # 飯田橋着: iidabashi_dep（実データ）優先、なければフォールバック
                    if e.get("iidabashi_dep"):
                        iidabashi_min = to_min(e["iidabashi_dep"])
                    else:
                        iidabashi_min = t_min + NAGATACHO_TO_IIDABASHI_FALLBACK
                    if best is None or t_min < to_min(best["nagatacho_dep"]):
                        best = {
                            "nagatacho_dep":     e["time"],
                            "nagatacho_dep_min": t_min,
                            "iidabashi_arr":     from_min(iidabashi_min),
                            "iidabashi_min":     iidabashi_min,
                            "transfer_line":     "有楽町線",
                        }

        if best:
            total_min = best["iidabashi_min"] - dep_min
            routes.append({
                "aobadai_dep":     dep_time,
                "aobadai_dep_min": dep_min,
                "train_type":      train_type,
                "dest":            dest,
                "nagatacho_arr":   from_min(nagatacho_min),
                "nagatacho_dep":   best["nagatacho_dep"],
                "iidabashi_arr":   best["iidabashi_arr"],
                "iidabashi_min":   best["iidabashi_min"],
                "transfer_line":   best["transfer_line"],
                "total_min":       total_min,
                "time_source":     time_source,
            })

    routes.sort(key=lambda r: r["aobadai_dep_min"])
    return routes


# ============================================================
# 検索インターフェース
# ============================================================
def _load_all(day_type: str, transfer_buffer: int):
    print("📡 時刻表読込中...")
    aobadai   = [t for t in load_aobadai_timetable(day_type) if t["type_ja"] == "各停"]
    print("📡 永田町→飯田橋 時刻表取得中...")
    nagatacho = fetch_nagatacho_timetable(day_type)
    routes    = build_routes(aobadai, nagatacho, transfer_buffer)
    return routes

def search_by_departure(dep_time: str, day_type: str, transfer_buffer: int,
                         num: int = 3) -> list[dict]:
    """青葉台出発時刻以降のルートを検索（各停のみ）"""
    routes  = _load_all(day_type, transfer_buffer)
    dep_min = to_min(dep_time)
    matched = [r for r in routes if r["aobadai_dep_min"] >= dep_min]
    return matched[:num]

def search_by_arrival(arr_time: str, day_type: str, transfer_buffer: int,
                       num: int = 3) -> list[dict]:
    """飯田橋到着時刻に間に合うルートを（遅出発順に）検索（各停のみ）"""
    routes  = _load_all(day_type, transfer_buffer)
    arr_min = to_min(arr_time)
    matched = [r for r in routes if r["iidabashi_min"] <= arr_min]
    matched.sort(key=lambda r: r["aobadai_dep_min"], reverse=True)
    return matched[:num]


# ============================================================
# 表示
# ============================================================
def print_routes(routes: list[dict], transfer_buffer: int):
    print()
    if not routes:
        print("❌ 該当する電車が見つかりませんでした。")
        return

    print("=" * 62)
    print("  青葉台 ─[田園都市線/半蔵門線]─ 永田町 ─[乗換]─ 飯田橋")
    print("=" * 62)

    for i, r in enumerate(routes, 1):
        # 永田町着が近似値の場合は注釈を付ける
        nagatacho_note = "" if r["time_source"] == "実測" else " ※概算"
        print(f"\n【案 {i}】")
        print(f"  🚉 青葉台  発: {r['aobadai_dep']}  {r['train_type']}  行先: {r['dest']}")
        print(f"  📍 永田町  着: {r['nagatacho_arr']}{nagatacho_note}  (半蔵門線 直通)")
        print(f"  ⏱  永田町  発: {r['nagatacho_dep']}  ({r['transfer_line']}に乗換)")
        print(f"  🏁 飯田橋  着: {r['iidabashi_arr']}")
        print(f"  ⏰ 所要時間: 約 {r['total_min']} 分  (乗換バッファ: {transfer_buffer}分)")

    print("\n" + "=" * 62)


# ============================================================
# メイン
# ============================================================
DAY_TYPE_LABELS = {
    "weekday":  "平日",
    "saturday": "土曜",
    "holiday":  "日曜・祝日",
}

def ask_day_type() -> str:
    print("ダイヤの種類を選んでください:")
    print("  1 : 平日")
    print("  2 : 土曜")
    print("  3 : 日曜・祝日")
    while True:
        c = input("\n選択 [1/2/3]: ").strip()
        if c == "1": return "weekday"
        if c == "2": return "saturday"
        if c == "3": return "holiday"
        print("  1、2、3 のいずれかを入力してください。")


def main():
    print("\n" + "=" * 62)
    print("  🚃  青葉台 → 永田町 → 飯田橋  乗換案内  v4")
    print("=" * 62)

    # ① 設定を読込み、変更があれば保存
    settings = load_settings()
    transfer_buffer = ask_transfer_buffer(settings["transfer_buffer_minutes"])
    if transfer_buffer != settings["transfer_buffer_minutes"]:
        settings["transfer_buffer_minutes"] = transfer_buffer
        save_settings(settings)
        print(f"  💾 設定を保存しました（乗換バッファ: {transfer_buffer}分）")

    # ② ダイヤ種別を選択
    print()
    day_type = ask_day_type()
    print(f"\n  ✅ {DAY_TYPE_LABELS[day_type]}ダイヤで検索します")

    # ③ 検索方法を選択
    print()
    print("検索方法を選んでください:")
    print("  1 : 出発時刻から検索（青葉台を何時に出発？）")
    print("  2 : 到着時刻から逆算（飯田橋に何時までに着きたい？）")
    choice = input("\n選択 [1/2]: ").strip()

    if choice == "1":
        t = input("青葉台の出発予定時刻 (例: 08:30): ").strip()
        results = search_by_departure(t, day_type, transfer_buffer)
        print_routes(results, transfer_buffer)

    elif choice == "2":
        t = input("飯田橋への到着希望時刻 (例: 09:30): ").strip()
        results = search_by_arrival(t, day_type, transfer_buffer)
        print_routes(results, transfer_buffer)

    else:
        print("1 または 2 を入力してください。")

if __name__ == "__main__":
    main()
