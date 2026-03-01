# AutoClip Pro — YouTube/Twitch Live 自動クリッパー

ライブ配信のチャットを監視し、盛り上がりを自動検知して OBS のリプレイバッファを保存・リネームする自動クリッピングツールです。

---

## システム概要

```
チャット → ChatMonitor → ExcitementDetector → 盛り上がり検知
                                                    ↓
                                           OBSController（リプレイ保存）
                                                    ↓
                                           ClipNamer（ファイルリネーム）
```

---

## 主なクラス・関数と役割

### `ExcitementDetector`
ライブチャットのコメントを分析し、盛り上がりを自動検知するエンジン。

| メソッド | 役割 |
|---|---|
| `add_comment()` | コメントを時間ウィンドウバッファに追加し、古いものを自動削除 |
| `check_excitement()` | コメント数が閾値超過 かつ クールダウン経過済みか判定 |
| `classify_genre()` | キーワード照合で「爆笑/称賛/失敗/困惑/Twitch文化」シーンを分類 |
| `extract_top_comment()` | リアクションワードを除外し、最頻出の意味あるコメントを抽出 |
| `_clean_text()` | 絵文字・顔文字を除去してテキストをサニタイズ |
| `_tokenize()` | テキストを単語トークンに分割（日本語は1コメント=1トークン） |

---

### `ClipNamer`
OBS が保存したリプレイバッファファイルを、意味のある名前にリネームする。

| メソッド | 役割 |
|---|---|
| `generate_filename()` | `HHMMSS_XmYs_トップコメント_ジャンル.mp4` 形式のファイル名を生成 |
| `wait_and_rename()` | ファイルサイズが安定するまで待機し、書き込み完了後にリネーム |
| `_sanitize()` | OS禁止文字（`\ / : * ? " < > |`）をファイル名から除去 |
| `_find_latest_mp4()` | 出力ディレクトリ内の最新 mp4/mkv ファイルを検出 |
| `_unique_path()` | 同名ファイルが存在する場合、連番を付加して重複を回避 |

---

### `OBSController`
OBS Studio との WebSocket v5 通信を担当するコントローラー。

| メソッド | 役割 |
|---|---|
| `connect()` | OBS WebSocket に接続し、録画出力先ディレクトリを自動取得 |
| `save_replay_buffer()` | OBS にリプレイバッファ保存命令を送信 |
| `disconnect()` | WebSocket 接続を切断してリソースを解放 |

---

### `ChatMonitor`
YouTube / Twitch のライブチャットをリアルタイム取得する監視スレッドを管理する。

| メソッド | 役割 |
|---|---|
| `start()` | チャット取得をデーモンスレッドとして起動 |
| `stop()` | 停止フラグを立ててスレッドを終了 |
| `_run()` | chat-downloader でコメントを受信し、ExcitementDetector に流すメインループ |

---

### SaaS プラン管理関数

| 関数 | 役割 |
|---|---|
| `load_settings()` | `autoclip_settings.json` からプラン情報・利用履歴を読み込む |
| `save_settings()` | 設定をJSONファイルに書き込み、次回起動時も状態を引き継ぐ |
| `is_holiday_pass_active()` | Holiday Pass が有効期間内か判定する |
| `check_daily_limit()` | Free版の「1日5回」上限を管理し、残り回数を返す |

---

### `AutoClipApp`（メインGUI）
システム全体を統括する Tkinter ベースのアプリケーション。

| メソッド | 役割 |
|---|---|
| `__init__()` | 設定読み込み・GUI構築・ログポーリング開始・起動時チェック |
| `_build_gui()` | 全UIウィジェット（URL入力・OBS設定・ボタン・ログ欄）を配置 |
| `_update_plan_label()` | プランステータスバーをリアルタイム更新 |
| `_start_monitoring()` | バリデーション → OBS接続 → ChatMonitor 起動 |
| `_stop_monitoring()` | 監視停止・OBS切断・ボタン状態リセット |
| `_on_comment()` | 各コメント受信時のコールバック（現在は処理なし） |
| `_on_excitement()` | 盛り上がり検知時のメインハンドラ（OBS保存→カウント更新→リネーム） |
| `_show_holiday_pass_offer()` | 初回クリップ後に Holiday Pass 獲得を案内するポップアップ |
| `_show_registration_window()` | メール・カード登録のダミーフォームを表示 |
| `_show_survey_window()` | 初期アンケート収集後に Holiday Pass（14日間）を有効化 |
| `_check_extension_prompt()` | 起動時に残り7日未満なら期間延長アンケートを提案 |
| `_show_extension_survey()` | 使用感アンケート収集後に Holiday Pass を7日延長 |
| `_open_output_folder()` | OBS出力先フォルダをOSのエクスプローラーで開く |
| `_log()` | スレッドセーフなキュー経由でログをGUI欄に送る |
| `_poll_log_queue()` | 50msごとにキューを処理してログ欄に表示・自動スクロール |

---

## SaaS プランの仕組み

| プラン | 対応プラットフォーム | 保存回数 | 期間 |
|---|---|---|---|
| Free版 | YouTube のみ | 5回/日 | 無期限 |
| Holiday Pass | YouTube + Twitch | 無制限 | 14日間（+7日延長可） |

**コンバージョンフロー:**
1. 初回クリップ保存 → Holiday Pass 獲得ポップアップ
2. メール・カード登録（ダミー）
3. 初期アンケート回答 → Holiday Pass 有効化（14日）
4. 残り7日未満で起動 → 使用感アンケート → 7日延長（最大21日）

---

## 必要な環境・ライブラリ

```
pip install -r requirements.txt
```

- `chat-downloader` — YouTube/Twitch チャット取得
- `obsws-python` — OBS WebSocket v5 通信

OBS Studio 側で **WebSocket サーバー**（ポート 4455）を有効にしてください。

---

## ブランチ構成

| ブランチ | 内容 |
|---|---|
| `main` | autoclip.py（プロトタイプ v1） |
| `feature/v2` | autoclip2.py |
| `feature/v3` | autoclip3.py |
| `feature/v4` | autoclip4.py |
| `feature/v5` | autoclip5.py |
