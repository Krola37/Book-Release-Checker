# セットアップ手順

## 1. Google Cloudでサービスアカウントを作成

1. [Google Cloud Console](https://console.cloud.google.com/) で新しいプロジェクトを作成（既存でもOK）
2. 「APIとサービス」→「ライブラリ」で以下の2つを有効化
   - **Google Sheets API**
   - **Google Calendar API**
3. 「APIとサービス」→「認証情報」→「認証情報を作成」→「サービスアカウント」を作成
4. 作成したサービスアカウントの「鍵」タブから「鍵を追加」→「JSON」を選択し、JSONキーファイルをダウンロード
5. サービスアカウントのメールアドレス（例：`xxxx@xxxx.iam.gserviceaccount.com`）を控えておく

## 2. スプレッドシート・カレンダーをサービスアカウントに共有

- **スプレッドシート**：対象シートの「共有」から、サービスアカウントのメールアドレスを**編集者**として追加
- **カレンダー**：Googleカレンダーの設定 →「特定のユーザーとの共有」から、サービスアカウントのメールアドレスを**予定の変更権限**で追加

## 3. GitHubリポジトリを作成

1. 新しいリポジトリを作成（Public推奨：Actionsの実行時間が無制限になるため。非公開情報はコードに含まれないので問題ありません）
2. 以下のファイルをリポジトリ直下に配置
   - `update_release_dates.py`
   - `requirements.txt`
   - `.github/workflows/update_release_dates.yml`（`update_release_dates.yml` をこのパスに配置）

## 4. GitHub Secretsを設定

リポジトリの「Settings」→「Secrets and variables」→「Actions」→「New repository secret」から、以下を1つずつ登録します。

| Secret名 | 値 |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ダウンロードしたJSONキーファイルの中身をそのまま貼り付け |
| `SPREADSHEET_ID` | スプレッドシートのURLの `/d/` と `/edit` の間の文字列 |
| `SHEET_NAME` | 対象シートのタブ名（例：`シート1`） |
| `CALENDAR_ID` | 対象のGoogleカレンダーのカレンダーID（例: `abcdef1234567890abcdef1234567890@group.calendar.google.com`。カレンダー設定の「カレンダーの統合」欄で確認できます） |
| `DISCORD_WEBHOOK_URL` | Discord Webhook URL |

## 5. 動作確認

1. リポジトリの「Actions」タブを開く
2. 左側の「Update Release Dates」ワークフローを選択
3. 「Run workflow」ボタンから手動実行し、ログを確認
4. スプレッドシート・カレンダー・Discordに正しく反映されているか確認

## 6. 定期実行の確認

`update_release_dates.yml` 内の `cron: "0 21 * * *"` により、毎日 日本時間6:00 に自動実行されます。時間を変えたい場合はこの値を調整してください（cronはUTC基準です）。

## 7. GAS側は不要に

このセットアップが完了すれば、Apps Scriptのトリガーは無効化・削除して問題ありません。スプレッドシートのA〜H列の構成はそのまま使えます（H列に手動でTRUEを入れれば、次回のワークフロー実行時に優先処理されます）。