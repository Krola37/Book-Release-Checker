"""
新刊リリース情報を取得し、Googleスプレッドシート・カレンダー・Discordに反映するスクリプト。
GAS版からの移植版（HTML解析をBeautifulSoup+検証済みロジックに置き換え、6分の実行時間制限を撤廃）。

必要な環境変数：
  GOOGLE_SERVICE_ACCOUNT_JSON  サービスアカウントのJSONキー（文字列そのまま）
  SPREADSHEET_ID               対象スプレッドシートのID
  SHEET_NAME                   対象シート名（省略時 "シート1"）
  CALENDAR_ID                  Googleカレンダーの CalendarID
  DISCORD_WEBHOOK_URL          Discord Webhook URL（任意。未設定なら通知はスキップ）

シート列構成（GAS版と同じ）：
  A: 作品タイトル   B: 最新巻数   C: 発売日   D: ジャンル
  E: ステータス      F: 最終更新日時   G: Discord通知フラグ   H: 手動更新フラグ
"""

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta

import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ------------------------------------------------------------
# 設定
# ------------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
]

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME = os.environ.get("SHEET_NAME", "シート1")
CALENDAR_ID = os.environ["CALENDAR_ID"]
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

GENRE_URLS = {
    "ライトノベル": "https://novel.sumikko.info/search/?k=",
    "文庫": "https://bunko.sumikko.info/search/?k=",
    "コミック": "https://comic.sumikko.info/search/?k=",
}

REQUEST_INTERVAL_SEC = 1.0  # サイトへの配慮（リクエスト間隔）
RESET_AFTER_DAYS = 5        # 「完了」を「未処理」に戻すまでの日数（GAS版と同じ）

DATE_VOL_PATTERN = re.compile(
    r"([0-9]{1,4})([0-9]{2})年([0-9]{1,2})月([0-9]{1,2})日\(([月火水木金土日])\)"
)
MARKER_PATTERN = re.compile(r"コミック(?!ス)")


# ------------------------------------------------------------
# 認証・APIクライアント
# ------------------------------------------------------------
def get_credentials():
    info = json.loads(SERVICE_ACCOUNT_JSON)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


# ------------------------------------------------------------
# タイトル・検索関連ユーティリティ
# ------------------------------------------------------------
def normalize_title(s):
    """タイトル比較用の正規化（全角/半角統一・空白除去・小文字化）"""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\s\u3000]+", "", s)
    return s.lower()


def simplify_title_for_search(title):
    """検索クエリ用にタイトルから記号類を除去"""
    title = re.sub(r"[<>{}\[\]【】「」『』（）()!！〜・、。:：;]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def extract_comic_items(plain_text):
    """
    ページ本文から「作品タイトル・巻数・発売日」の一覧を抽出する。

    手順：
      1. "コミック"（"コミックス"というレーベル表記の一部は除外）の出現位置を収集
         → 各アイテムの「タイトル開始位置」の目印
      2. "巻数＋年＋月＋日＋(曜日)" パターンの出現位置を収集
         → 各アイテムの「タイトル終了位置（＝巻数の開始位置）」
      3. 日付パターンごとに直前の最も近いマーカー位置からの区間を
         タイトルとして切り出す
    """
    marker_positions = [m.end() for m in MARKER_PATTERN.finditer(plain_text)]

    items = []
    for dv in DATE_VOL_PATTERN.finditer(plain_text):
        marker_pos = None
        for pos in reversed(marker_positions):
            if pos < dv.start():
                marker_pos = pos
                break
        if marker_pos is None:
            continue

        raw_title = plain_text[marker_pos:dv.start()].strip()
        if not raw_title or len(raw_title) > 40:
            continue

        items.append(
            {
                "title": raw_title,
                "volume": int(dv.group(1)),
                "year": 2000 + int(dv.group(2)),
                "month": int(dv.group(3)),
                "day": int(dv.group(4)),
                "weekday": dv.group(5),
            }
        )
    return items


def fetch_search_items(genre, search_title):
    base_url = GENRE_URLS.get(genre, GENRE_URLS["コミック"])
    query = simplify_title_for_search(search_title)
    url = base_url + requests.utils.quote(query)

    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    plain_text = unicodedata.normalize("NFKC", soup.get_text())
    return extract_comic_items(plain_text)


# ------------------------------------------------------------
# 通知・カレンダー
# ------------------------------------------------------------
def send_discord(title, volume, release_date_str, genre):
    if not DISCORD_WEBHOOK_URL:
        return

    emoji = "📚"
    if genre == "コミック":
        emoji = "🎨"
    elif genre in ("ライトノベル", "文庫"):
        emoji = "📖"

    message = (
        f"{emoji} **新刊の発売情報を見つけました！**\n"
        f"----------------------------\n"
        f"・ **作品名:** {title}\n"
        f"・ **巻数:** 第{volume}巻\n"
        f"・ **発売日:** {release_date_str}\n"
        f"・ **ジャンル:** {genre}\n"
        f"----------------------------"
    )
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        print(f"❌ Discord送信エラー: {e}")


def create_calendar_event(calendar_service, title, volume, release_date):
    event = {
        "summary": f"{title} 第{volume}巻",
        "description": f"作品タイトル：{title}\n巻数：{volume}巻",
        "start": {"date": release_date.strftime("%Y-%m-%d")},
        "end": {"date": (release_date + timedelta(days=1)).strftime("%Y-%m-%d")},
    }
    calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()


def is_true(value):
    return str(value).strip().upper() == "TRUE"


def parse_date_flexible(s):
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"日付形式を認識できません: {s}")


# ------------------------------------------------------------
# メイン処理
# ------------------------------------------------------------
def process_manual_rows(ws, calendar_service, rows):
    """H列（手動更新フラグ）がONの行を最優先で処理する"""
    for i, row in enumerate(rows[1:], start=2):
        title = row[0].strip() if len(row) > 0 else ""
        is_manual_trigger = row[7].strip() if len(row) > 7 else ""
        if not title or not is_true(is_manual_trigger):
            continue

        try:
            current_latest_volume = row[1].strip() if len(row) > 1 else ""
            release_date_str = row[2].strip() if len(row) > 2 else ""
            genre = row[3].strip() if len(row) > 3 else ""
            is_notify_target = row[6].strip() if len(row) > 6 else ""

            if not current_latest_volume or not release_date_str:
                print(f"⚠️ Skip (Row {i}): B列またはC列が未入力のため手動スキップします。")
                continue

            release_date = parse_date_flexible(release_date_str)
            matched_volume = int(current_latest_volume)

            create_calendar_event(calendar_service, title, matched_volume, release_date)

            if is_true(is_notify_target):
                send_discord(title, matched_volume, release_date.strftime("%Y/%m/%d"), genre)

            ws.update_cell(i, 5, "完了")
            ws.update_cell(i, 6, datetime.now().strftime("%Y/%m/%d %H:%M:%S"))
            ws.update_cell(i, 8, False)

            print(f"✅ 【手動登録成功】 Row {i}: {title} を処理しました。")

        except Exception as e:
            print(f"❌ 手動登録エラー（Row {i}）: {e}")


def process_auto_rows(ws, calendar_service, rows):
    for i, row in enumerate(rows[1:], start=2):
        title = row[0].strip() if len(row) > 0 else ""
        current_latest_volume = row[1].strip() if len(row) > 1 else ""
        genre = row[3].strip() if len(row) > 3 else ""
        status = row[4].strip() if len(row) > 4 else ""
        is_notify_target = row[6].strip() if len(row) > 6 else ""

        if not title or (status and status != "未処理"):
            continue

        try:
            m = re.match(r"(.+?)([0-9]+)$", title)
            search_title = m.group(1).strip() if m else title

            items = fetch_search_items(genre, search_title)
            normalized_search = normalize_title(search_title)
            exact_matches = [it for it in items if normalize_title(it["title"]) == normalized_search]

            if exact_matches:
                best = max(exact_matches, key=lambda it: it["volume"])
                matched_volume = best["volume"]
                release_date = datetime(best["year"], best["month"], best["day"])
                current_vol_num = int(current_latest_volume) if current_latest_volume.isdigit() else 0

                if matched_volume > current_vol_num:
                    ws.update_cell(i, 2, matched_volume)
                    ws.update_cell(i, 3, release_date.strftime("%Y/%m/%d"))
                    ws.update_cell(i, 5, "完了")
                    ws.update_cell(i, 6, datetime.now().strftime("%Y/%m/%d %H:%M:%S"))

                    create_calendar_event(calendar_service, title, matched_volume, release_date)

                    if is_true(is_notify_target):
                        send_discord(title, matched_volume, release_date.strftime("%Y/%m/%d"), genre)
                        print(f"🔔 登録＆Discord通知送信: {title} 第{matched_volume}巻")
                    else:
                        print(f"📅 カレンダー登録のみ（通知OFF）: {title} 第{matched_volume}巻")
                else:
                    print(f"⏭ 最新巻数の更新なし: {title}")
                    ws.update_cell(i, 5, "完了")
                    ws.update_cell(i, 6, datetime.now().strftime("%Y/%m/%d %H:%M:%S"))
            else:
                print(f"🔍 完全一致する作品が見つかりませんでした: {title}（検索クエリ: {search_title}）")
                ws.update_cell(i, 5, "完了")
                ws.update_cell(i, 6, datetime.now().strftime("%Y/%m/%d %H:%M:%S"))

        except Exception as e:
            print(f"❌ エラー（Row {i}）: {e}")

        time.sleep(REQUEST_INTERVAL_SEC)


def reset_old_processed_rows(ws):
    rows = ws.get_all_values()
    today = datetime.now()
    for i, row in enumerate(rows[1:], start=2):
        status = row[4].strip() if len(row) > 4 else ""
        last_update_str = row[5].strip() if len(row) > 5 else ""
        if status != "完了" or not last_update_str:
            continue
        try:
            last_update = datetime.strptime(last_update_str, "%Y/%m/%d %H:%M:%S")
        except ValueError:
            continue
        if (today - last_update).days >= RESET_AFTER_DAYS:
            ws.update_cell(i, 5, "未処理")


def main():
    creds = get_credentials()
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    calendar_service = build("calendar", "v3", credentials=creds)

    print("⚡ 手動更新チェック（H列）の確認を開始します...")
    rows = ws.get_all_values()
    process_manual_rows(ws, calendar_service, rows)

    print("🔁 通常巡回処理を開始します...")
    rows = ws.get_all_values()  # 手動処理での更新を反映するため再取得
    process_auto_rows(ws, calendar_service, rows)

    print("🔁 5日以上前に完了した行を未処理に戻します...")
    reset_old_processed_rows(ws)

    print("✅ 全処理が完了しました。")


if __name__ == "__main__":
    main()