"""
新刊リリース情報を取得し、Googleスプレッドシート・カレンダー・Discordに反映するスクリプト。
GAS版からの移植版（HTML解析をBeautifulSoup+検証済みロジックに置き換え、6分の実行時間制限を撤廃）。

必要な環境変数：
  GOOGLE_SERVICE_ACCOUNT_JSON  サービスアカウントのJSONキー（文字列そのまま）
  SPREADSHEET_ID               対象スプレッドシートのID
  SHEET_NAME                   対象シート名（省略時 "シート1"）
  CALENDAR_ID                  Googleカレンダーの CalendarID
  DISCORD_WEBHOOK_URL          Discord Webhook URL（任意。未設定なら通知はスキップ）
  DAILY_LIMIT                  1日あたりの自動チェック件数上限（任意。省略時40）

シート列構成（GAS版と同じ）：
  A: 作品タイトル   B: 最新巻数   C: 発売日   D: ジャンル
  E: ステータス（最終処理結果の表示用。処理対象の判定には使わない）
  F: 最終更新日時（この日時が古い行から優先的にチェックされる）
  G: Discord通知フラグ   H: 手動更新フラグ
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

# 新刊は数カ月おきにしか出ないため、毎日全件チェックする必要はない。
# 1日あたりのチェック件数に上限を設け、最終更新日時が古い行から
# 優先的に処理することで、リクエスト数・実行時間・APIクォータを節約する。
# 例：200件登録時、DAILY_LIMIT=40なら約5日で全件が一巡する。
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT") or "40")

# GitHub ActionsのランナーIPが一時的に不安定なことがあるため、
# 接続失敗時はリトライする（診断の結果、requestsライブラリ自体やUser-Agentは
# 原因ではなく、実行のたびに変わるランナーIPの一時的な問題と判明したため）
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


# ------------------------------------------------------------
# 認証・APIクライアント
# ------------------------------------------------------------
def get_credentials():
    info = json.loads(SERVICE_ACCOUNT_JSON)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


# ------------------------------------------------------------
# タイトル・検索関連ユーティリティ
# ------------------------------------------------------------

# 波ダッシュ「〜」(U+301C)、全角チルダ「～」(U+FF5E)、
# スウングダッシュ「⁓」(U+2053) はすべて見た目が似ているが別の文字コードで、
# NFKC正規化では統一されない（全角チルダのみASCIIの「~」に変換され、
# 波ダッシュは変換対象外）。サブタイトルを「〜」で囲む作品名が非常に多く、
# サイト側の表記とスプレッドシート側の表記でこれらの文字が食い違うと
# 完全一致判定に失敗するため、比較前にすべて同じ文字へ統一する。
TILDE_VARIANTS_PATTERN = re.compile(r"[\u301C\uFF5E\u2053~]")


def normalize_title(s):
    """タイトル比較用の正規化（全角/半角統一・波ダッシュ類統一・空白除去・小文字化）"""
    s = unicodedata.normalize("NFKC", s)
    s = TILDE_VARIANTS_PATTERN.sub("~", s)
    s = re.sub(r"[\s\u3000]+", "", s)
    return s.lower()


def simplify_title_for_search(title):
    """検索クエリ用にタイトルから記号類を除去"""
    title = re.sub(r"[<>{}\[\]【】「」『』（）()!！〜～・、。:：;]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


ITEM_NAME_VOLUME_PATTERN = re.compile(r"^(.*?)[\s　]*([0-9]+)$")
DATE_PATTERN = re.compile(r"([0-9]{2})年([0-9]{1,2})月([0-9]{1,2})日\(([月火水木金土日])\)")

# ジャンル（スプレッドシートD列）ごとに、type-tag（カテゴリ表示）の
# 許可リストを切り替える（ホワイトリスト方式）。
# サイトが検索対象のジャンルによって別ドメイン（comic./novel./bunko.）に
# 分かれているのに加え、同じサイト内でもtype-tagの値がジャンルによって
# 異なる（例: コミックサイトは「コミック」、ライトノベルサイトは
# 「ライトノベル」「TL」「ジュニアノベル」など複数）ため、
# ジャンルごとに許可するtype-tagを個別に定義する。
#
# 実データで確認済みの値：
#   コミック     : "コミック"
#   ライトノベル : "ライトノベル" / "TL" / "ジュニアノベル"（実データで確認済み）
#   文庫         : 未確認（"文庫"を仮設定。誤除外が出たら調整）
#
# ライトノベルの "TL"（ティーンズラブ）・"ジュニアノベル"（映画ノベライズ等）を
# 対象に含めたくない場合は、該当ジャンルのsetから取り除いてください。
ALLOWED_CATEGORIES = {
    "コミック": {"コミック"},
    "ライトノベル": {"ライトノベル", "TL", "ジュニアノベル"},
    "文庫": {"文庫"},
}


def extract_items(html, allowed_categories):
    """
    検索結果ページのHTMLから「作品タイトル・巻数・発売日・カテゴリ」の
    一覧を抽出する（実際のタグ構造に基づく解析）。

    ページ構造（実データで確認済み）：
      <li class="item">
        <div class="Types"><span class="type type-tag">コミック</span></div>
        <div class="name">ONE PIECE 115</div>
        <div class="sab"><span>26年7月3日(金)</span><span>尾田栄一郎</span></div>
        ...
      </li>

    type-tag が allowed_categories に含まれないもの（グッズ・関連商品・
    対象外ジャンルの商品）は除外する。
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []

    for li in soup.select("li.item"):
        type_tag = li.select_one(".type-tag")
        category = type_tag.get_text(strip=True) if type_tag else ""
        if category not in allowed_categories:
            continue  # ホワイトリスト外（グッズ・対象外ジャンル）を除外

        name_div = li.select_one(".name")
        if not name_div:
            continue
        name_text = name_div.get_text(strip=True)

        sab_divs = li.select(".sab")
        if not sab_divs:
            continue
        date_span = sab_divs[0].find("span")
        if not date_span:
            continue
        date_text = date_span.get_text(strip=True)

        dm = DATE_PATTERN.match(date_text)
        if not dm:
            continue

        vm = ITEM_NAME_VOLUME_PATTERN.match(name_text)
        if vm:
            title = vm.group(1).strip()
            volume = int(vm.group(2))
        else:
            # 巻数の数字が末尾にない（単発作品など）はスキップ対象にする
            continue

        items.append(
            {
                "title": title,
                "volume": volume,
                "year": 2000 + int(dm.group(1)),
                "month": int(dm.group(2)),
                "day": int(dm.group(3)),
                "weekday": dm.group(4),
                "category": category,
            }
        )
    return items


def fetch_search_items(genre, search_title):
    base_url = GENRE_URLS.get(genre, GENRE_URLS["コミック"])
    allowed_categories = ALLOWED_CATEGORIES.get(genre, ALLOWED_CATEGORIES["コミック"])
    query = simplify_title_for_search(search_title)
    url = base_url + requests.utils.quote(query)

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # (connect timeout, read timeout) を分離。GitHub Actionsの
            # ランナーIPが一時的に不安定なことがあるため、接続段階は
            # 短めに切って早くリトライに回す。
            resp = requests.get(url, timeout=(10, 30), headers=HEADERS)
            resp.encoding = "utf-8"
            return extract_items(resp.text, allowed_categories)
        except requests.exceptions.RequestException as e:
            last_error = e
            print(f"⚠️ 接続失敗（{attempt}/{MAX_RETRIES}回目, {url}）: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)

    raise last_error


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


def batch_update_row(ws, row_index, values):
    """
    1行分の複数セルを1回のAPI呼び出しでまとめて更新する。
    Sheets APIのクォータ（1分あたりのリクエスト数）を節約するため、
    update_cellを複数回呼ぶ代わりにこちらを使う。

    values: {列番号: 値} の辞書。例: {2: 5, 3: "2026/03/04", 5: "完了"}
    """
    cells = [gspread.Cell(row=row_index, col=col, value=value) for col, value in values.items()]
    ws.update_cells(cells)


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

            batch_update_row(
                ws,
                i,
                {5: "完了", 6: datetime.now().strftime("%Y/%m/%d %H:%M:%S"), 8: False},
            )

            print(f"✅ 【手動登録成功】 Row {i}: {title} を処理しました。")

        except Exception as e:
            print(f"❌ 手動登録エラー（Row {i}）: {e}")


def select_rows_to_check(rows, daily_limit):
    """
    「最終更新日時（F列）が古い行」から優先的にdaily_limit件だけ選ぶ。
    新刊は数カ月おきにしか出ないため、毎日全件チェックする必要はない。
    1日あたりの処理件数に上限を設けることで、リクエスト数・実行時間・
    Sheets APIクォータを大きく節約しつつ、数日〜1週間程度で全件を
    一巡させる。
    """
    candidates = []
    for i, row in enumerate(rows[1:], start=2):
        title = row[0].strip() if len(row) > 0 else ""
        if not title:
            continue
        last_update_str = row[5].strip() if len(row) > 5 else ""
        try:
            last_update = datetime.strptime(last_update_str, "%Y/%m/%d %H:%M:%S")
        except (ValueError, IndexError):
            last_update = datetime.min  # 未チェックの行は最優先で処理する
        candidates.append((last_update, i, row))

    candidates.sort(key=lambda x: x[0])  # 古い順（未チェック優先）
    return [(i, row) for _, i, row in candidates[:daily_limit]]


def process_auto_rows(ws, calendar_service, rows, daily_limit):
    targets = select_rows_to_check(rows, daily_limit)
    print(f"📋 今回チェック対象: {len(targets)}件（全{len(rows) - 1}件中）")

    for i, row in targets:
        title = row[0].strip() if len(row) > 0 else ""
        current_latest_volume = row[1].strip() if len(row) > 1 else ""
        genre = row[3].strip() if len(row) > 3 else ""
        is_notify_target = row[6].strip() if len(row) > 6 else ""

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
                    batch_update_row(
                        ws,
                        i,
                        {
                            2: matched_volume,
                            3: release_date.strftime("%Y/%m/%d"),
                            5: "完了",
                            6: datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
                        },
                    )

                    create_calendar_event(calendar_service, title, matched_volume, release_date)

                    if is_true(is_notify_target):
                        send_discord(title, matched_volume, release_date.strftime("%Y/%m/%d"), genre)
                        print(f"🔔 登録＆Discord通知送信: {title} 第{matched_volume}巻")
                    else:
                        print(f"📅 カレンダー登録のみ（通知OFF）: {title} 第{matched_volume}巻")
                else:
                    print(f"⏭ 最新巻数の更新なし: {title}")
                    batch_update_row(
                        ws, i, {5: "完了", 6: datetime.now().strftime("%Y/%m/%d %H:%M:%S")}
                    )
            else:
                print(f"🔍 完全一致する作品が見つかりませんでした: {title}（検索クエリ: {search_title}）")
                batch_update_row(
                    ws, i, {5: "完了", 6: datetime.now().strftime("%Y/%m/%d %H:%M:%S")}
                )

        except Exception as e:
            print(f"❌ エラー（Row {i}）: {e}")

        time.sleep(REQUEST_INTERVAL_SEC)


def main():
    creds = get_credentials()
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    calendar_service = build("calendar", "v3", credentials=creds)

    print("⚡ 手動更新チェック（H列）の確認を開始します...")
    rows = ws.get_all_values()
    process_manual_rows(ws, calendar_service, rows)

    print(f"🔁 通常巡回処理を開始します（1日あたり最大{DAILY_LIMIT}件）...")
    rows = ws.get_all_values()  # 手動処理での更新を反映するため再取得
    process_auto_rows(ws, calendar_service, rows, DAILY_LIMIT)

    print("✅ 全処理が完了しました。")


if __name__ == "__main__":
    main()