"""
青葉台駅（田園都市線・渋谷方面）時刻表スクレイパー v2
取得元: NAVITIME (https://www.navitime.co.jp)
対象ダイヤ: 平日 / 土曜 / 日曜祝日
出力: aobadai_timetable.json

v2 追加:
- 各停列車の列車別詳細ページから永田町着時刻を正確に取得
"""

import asyncio
import json
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from datetime import datetime, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

# NAVITIME の青葉台・田園都市線・渋谷方面（駅時刻表）
STATION_URL = "https://www.navitime.co.jp/diagram/timetable?node=00004993&lineId=00000789&updown=0"

# 列車詳細ページのURL雛形
TRAIN_DETAIL_BASE = "https://www.navitime.co.jp/diagram/stops/00000789/{train_id}/?node=00004993&year={year}&month={month:02d}&day={day:02d}"

# 永田町のNAVITIMEノードID
NAGATACHO_NODE = "00000665"

# 種別マッピング
TRAIN_TYPE_MAP = {
    "急":  {"ja": "急行",  "en": "Express"},
    "準":  {"ja": "準急",  "en": "SemiExpress"},
    "特":  {"ja": "特急",  "en": "LimitedExpress"},
    "通":  {"ja": "通急",  "en": "ThroughExpress"},
    "":    {"ja": "各停",  "en": "Local"},
}

# 各ダイヤ種別に対応する直近の日付を取得
def get_date_for_day_type(day_type: str) -> datetime:
    today = datetime.now()
    d = today + timedelta(days=1)
    if day_type == "weekday":
        while d.weekday() >= 5:      # 月〜金を探す
            d += timedelta(days=1)
    elif day_type == "saturday":
        while d.weekday() != 5:      # 土曜を探す
            d += timedelta(days=1)
    else:                            # holiday
        while d.weekday() != 6:      # 日曜を探す
            d += timedelta(days=1)
    return d


async def scrape_one_page(page) -> list[dict]:
    """現在 display:block の time-table-frame から時刻表を取得（TrainIDも抽出）"""
    await page.wait_for_timeout(2000)

    trains = await page.evaluate("""() => {
        const result = [];

        // display:block（visible）な time-table-frame を選択
        const allFrames = document.querySelectorAll('.time-table-frame');
        let activeFrame = null;
        for (const f of allFrames) {
            if (window.getComputedStyle(f).display !== 'none') {
                activeFrame = f;
                break;
            }
        }
        if (!activeFrame) return result;

        const hourEls = activeFrame.querySelectorAll('.diagram-frame__hour');
        const minEls  = activeFrame.querySelectorAll('.diagram-frame__min');

        hourEls.forEach((hourEl, idx) => {
            const hour = hourEl.innerText.trim();
            if (!hour || !/^\\d+$/.test(hour)) return;

            const minContainer = minEls[idx];
            if (!minContainer) return;

            const timeFrames = minContainer.querySelectorAll('.time-frame');
            timeFrames.forEach(frame => {
                const anchor = frame.querySelector('a');
                if (!anchor) return;

                const rubyTop  = anchor.querySelector('.ruby-top');
                const timeSpan = anchor.querySelector('.time');
                const destSpan = anchor.querySelector('.ruby-dest');

                const typeRaw = rubyTop  ? rubyTop.innerText.trim().replace(/\\n/g,'') : '';
                const minute  = timeSpan ? timeSpan.innerText.trim() : '';
                const dest    = destSpan ? destSpan.innerText.trim() : '';

                // 列車詳細ページへのhrefからTrainIDを抽出
                const href = anchor.getAttribute('href') || '';
                const trainMatch = href.match(/\\/diagram\\/stops\\/\\d+\\/([0-9a-f]+)\\//);
                const trainId = trainMatch ? trainMatch[1] : '';

                if (minute) {
                    result.push({ hour, minute, typeRaw, dest, trainId });
                }
            });
        });
        return result;
    }""")
    return trains


async def get_nagatacho_arrival(page) -> str | None:
    """列車詳細ページから永田町の到着時刻を取得"""
    try:
        result = await page.evaluate("""(nagatacho_node) => {
            // 方法1: 永田町のノードIDを含むリンクから探す
            const links = document.querySelectorAll('a[href*="node=' + nagatacho_node + '"]');
            for (const link of links) {
                const container = link.closest('li') || link.closest('tr') || link.parentElement;
                if (container) {
                    const text = container.textContent || '';
                    const match = text.match(/(\\d{2}:\\d{2})着/);
                    if (match) return match[1];
                }
            }

            // 方法2: 「永田町」テキストを含む要素を探す
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                if (node.textContent.includes('永田町')) {
                    const parent = node.parentElement;
                    const container = parent.closest('li') || parent.closest('div') || parent.parentElement;
                    if (container) {
                        const match = container.textContent.match(/(\\d{2}:\\d{2})着/);
                        if (match) return match[1];
                    }
                }
            }
            return null;
        }""", NAGATACHO_NODE)
        return result
    except Exception:
        return None


async def enrich_with_nagatacho(page, trains: list[dict], date: datetime) -> None:
    """各停・直通列車の nagatacho_arr を列車詳細ページから取得して付与（in-place）"""
    targets = [
        t for t in trains
        if t["type_ja"] == "各停"
        and t.get("train_id")
        and t["dest"].strip() != "渋谷"   # 渋谷止まりはスキップ
    ]

    print(f"   🔍 永田町着時刻を列車別に取得中 ({len(targets)} 本)...")
    success = 0
    skip_shibuya = len(trains) - len(targets)

    for i, train in enumerate(targets, 1):
        url = TRAIN_DETAIL_BASE.format(
            train_id=train["train_id"],
            year=date.year, month=date.month, day=date.day
        )
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            arr = await get_nagatacho_arrival(page)
            train["nagatacho_arr"] = arr
            if arr:
                success += 1
        except Exception:
            train["nagatacho_arr"] = None

        # 進捗表示（25本ごと）
        if i % 25 == 0 or i == len(targets):
            print(f"      {i}/{len(targets)} 完了...")

        # レート制限回避
        await asyncio.sleep(0.8)

    # 渋谷止まり列車は nagatacho_arr = None に設定
    for t in trains:
        if "nagatacho_arr" not in t:
            t["nagatacho_arr"] = None

    print(f"   ✅ 永田町着時刻: {success}/{len(targets)} 本 取得成功")
    if skip_shibuya:
        print(f"   ⏭  渋谷止まり: {skip_shibuya} 本（永田町通過なし）")


async def scrape_all():
    """平日・土曜・日曜祝日の時刻表を全取得 + 永田町着時刻付与"""
    print("=" * 50)
    print("  青葉台駅 時刻表スクレイピング開始 v2")
    print(f"  取得元: NAVITIME")
    print(f"  実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    timetable = {}
    day_configs = [
        ("weekday",  "平日"),
        ("saturday", "土曜"),
        ("holiday",  "日曜・祝日"),
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )

        async def click_tab_and_scrape(tab_text: str) -> list[dict]:
            btn = page.locator(f"li:has-text('{tab_text}')").first
            await btn.click()
            await page.wait_for_timeout(2500)
            return await scrape_one_page(page)

        # ── Step1: 3ダイヤの駅時刻表を取得 ──────────────────────
        print("\n[ Step 1 ] 駅時刻表スクレイピング")
        raw = {}
        await page.goto(STATION_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        for day_key, day_label in day_configs:
            print(f"\n📅 {day_label} ダイヤ取得中...")
            trains = await click_tab_and_scrape(day_label.split("・")[0])  # 「日曜」でクリック
            formatted = _format(trains)
            raw[day_key] = formatted
            local_count = sum(1 for t in formatted if t["type_ja"] == "各停")
            print(f"   → 合計 {len(trains)} 本（各停 {local_count} 本）")

        # ── Step2: 各ダイヤの各停列車に永田町着時刻を付与 ────────
        print("\n[ Step 2 ] 列車別 永田町着時刻を取得")
        for day_key, day_label in day_configs:
            date = get_date_for_day_type(day_key)
            print(f"\n📅 {day_label}（{date.strftime('%Y/%m/%d')} 相当）")
            await enrich_with_nagatacho(page, raw[day_key], date)
            timetable[day_key] = raw[day_key]

        await browser.close()

    return timetable


def _format(trains: list[dict]) -> list[dict]:
    """生データを整形（時刻文字列・種別を付与）"""
    result = []
    for t in trains:
        hour   = t["hour"].zfill(2)
        minute = t["minute"].zfill(2)
        time_str = f"{hour}:{minute}"

        type_raw = t["typeRaw"].replace("\n", "").strip()
        type_info = TRAIN_TYPE_MAP.get(type_raw, TRAIN_TYPE_MAP[""])

        result.append({
            "time":     time_str,
            "type_ja":  type_info["ja"],
            "type_en":  type_info["en"],
            "dest":     t["dest"],
            "train_id": t.get("trainId", ""),
        })

    def sort_key(x):
        h, m = map(int, x["time"].split(":"))
        return (h + 24 if h < 5 else h) * 60 + m

    result.sort(key=sort_key)
    return result


def save_json(timetable: dict, path: str = "aobadai_timetable.json"):
    """JSONファイルに保存"""
    output = {
        "station":    "青葉台",
        "line":       "東急田園都市線",
        "direction":  "渋谷方面",
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source":     "NAVITIME",
        "timetable":  timetable,
    }
    Path(path).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 保存完了: {path}")

    for day, trains in timetable.items():
        types = {}
        for t in trains:
            types[t["type_ja"]] = types.get(t["type_ja"], 0) + 1
        type_summary = ", ".join(f"{k}:{v}本" for k, v in types.items())
        nagatacho_ok = sum(1 for t in trains if t.get("nagatacho_arr"))
        print(f"   {day}: 計{len(trains)}本  ({type_summary})  永田町時刻取得済み: {nagatacho_ok}本")


async def main():
    timetable = await scrape_all()
    output_path = str(Path(__file__).parent / "aobadai_timetable.json")
    save_json(timetable, output_path)
    return timetable


if __name__ == "__main__":
    asyncio.run(main())
