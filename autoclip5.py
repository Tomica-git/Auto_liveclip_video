from __future__ import annotations  # Python 3.9 互換
import threading
import time
import json
import re
import os
import sys
from collections import deque, Counter
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# 外部ライブラリのインポート
try:
    from chat_downloader import ChatDownloader
except ImportError:
    print("❌ chat-downloader が見つかりません。pip install chat-downloader を実行してください。")
    sys.exit(1)

# obsws-python はProモードでのみ必要なので遅延インポート
OBS_AVAILABLE = False
try:
    from obsws_python import ReqClient
    OBS_AVAILABLE = True
except ImportError:
    pass

# ==========================================
# 1. 設定・定数定義
# ==========================================
SETTINGS_FILE = "autoclip_settings.json"
HIGHLIGHTS_FILE = "highlights.txt"

NG_GENRE_DICT = {
    "爆笑": ["w", "ww", "www", "草", "竹", "腹痛い", "ワロタ", "lol", "lmao", "笑"],
    "称賛": ["神", "うっま", "うまい", "8888", "すごい", "nice", "god", "最高", "かっこいい"],
    "失敗": ["あ", "ああ", "PON", "トロール", "やらかした", "ドンマイ", "F", "rip", "ミス"],
    "困惑": ["！？", "？？？", "は？", "ま？", "?", "what", "why", "bug", "バグ"],
    "Twitch": ["LUL", "Pog", "Kappa", "KEKW", "monkaS", "Pepega", "Sadge", "EZ"]
}

# ==========================================
# 2. ユーティリティ関数
# ==========================================

def clean_url(raw_url: str) -> str:
    """URLから不要なトラッキングパラメータを削除し、/live/ID を /watch?v=ID に変換する"""
    parsed = urlparse(raw_url)

    # /live/VIDEO_ID 形式 → /watch?v=VIDEO_ID に変換
    live_match = re.match(r'^/live/([a-zA-Z0-9_-]+)', parsed.path)
    if live_match:
        video_id = live_match.group(1)
        parsed = parsed._replace(
            path='/watch',
            query=f'v={video_id}' + ('&' + parsed.query if parsed.query else '')
        )

    # ゴミパラメータを削除
    query = parse_qs(parsed.query)
    trash_params = ['si', 'feature', 'ab_channel']
    for param in trash_params:
        query.pop(param, None)

    new_query = urlencode(query, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path,
                       parsed.params, new_query, parsed.fragment))


def format_timestamp(seconds: float) -> str:
    """秒数を [HH:MM:SS] 形式に変換する"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02}:{m:02}:{s:02}"


# ==========================================
# 3. ロジッククラス
# ==========================================

class SettingsManager:
    """設定の読み込み・保存・プラン判定を担当"""

    def __init__(self):
        self.data = {
            "is_pro": False,
            "holiday_pass_expiry": None,
            "daily_saves": 0,
            "last_save_date": "",
            "tutorial_done": False,
        }
        self.load()

    def load(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    self.data.update(json.load(f))
            except Exception:
                pass
        self._check_daily_reset()

    def save(self):
        with open(SETTINGS_FILE, "w") as f:
            json.dump(self.data, f)

    def _check_daily_reset(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.data["last_save_date"] != today:
            self.data["last_save_date"] = today
            self.data["daily_saves"] = 0
            self.save()

    def can_save(self) -> bool:
        self._check_daily_reset()
        if self._is_pro_active():
            return True
        return self.data["daily_saves"] < 5

    def remaining_saves(self) -> int:
        self._check_daily_reset()
        if self._is_pro_active():
            return 999  # 実質無制限
        return max(0, 5 - self.data["daily_saves"])

    def increment_save_count(self):
        self.data["daily_saves"] += 1
        self.save()

    def _is_pro_active(self) -> bool:
        if self.data.get("is_pro"):
            return True
        expiry_str = self.data.get("holiday_pass_expiry")
        if expiry_str:
            try:
                expiry = datetime.fromisoformat(expiry_str)
                if datetime.now() < expiry:
                    return True
            except ValueError:
                pass
        return False


# ------------------------------------------
# ExcitementDetector: 盛り上がり検知の基底ロジック
# ------------------------------------------
class ExcitementDetector:
    """コメントの盛り上がり検知 — サブクラスで時間基準を切り替える"""

    def __init__(self, threshold: int = 10, cooldown: float = 60.0):
        self.comments: deque = deque()
        self.threshold_count = threshold
        self.cooldown_time = cooldown
        self.last_trigger_time: float = -9999.0
        self.ng_regex = re.compile(
            r'[\U00010000-\U0010ffff]'   # サロゲートペア絵文字
            r'|[\u2000-\u2fff]'          # 特殊記号
            r'|[:;][a-zA-Z0-9_]+[:;]'    # :emoji: 形式
            r'|\(.*?\)'                  # 半角カッコ顔文字
            r'|（.*?）'                   # 全角カッコ顔文字
        )

    # --- サブクラスでオーバーライド ---
    def _current_time(self) -> float:
        raise NotImplementedError

    def add_comment(self, text: str, timestamp: float) -> None:
        """コメントを追加し、30秒ウィンドウ外の古いコメントを除去"""
        self.comments.append((timestamp, text))
        cutoff = timestamp - 30.0
        while self.comments and self.comments[0][0] < cutoff:
            self.comments.popleft()

    def check_excitement(self, current_time: float) -> dict | None:
        """閾値を超えていたら盛り上がり情報を返す"""
        if current_time - self.last_trigger_time < self.cooldown_time:
            return None
        if len(self.comments) >= self.threshold_count:
            genre = self._determine_genre()
            top_comment = self._extract_top_comment()
            self.last_trigger_time = current_time
            return {
                "genre": genre,
                "top_comment": top_comment,
                "count": len(self.comments),
            }
        return None

    def _determine_genre(self) -> str:
        counts = {k: 0 for k in NG_GENRE_DICT}
        total = len(self.comments)
        for _, text in self.comments:
            for genre, words in NG_GENRE_DICT.items():
                if any(w in text for w in words):
                    counts[genre] += 1
        best = max(counts, key=counts.get)
        if counts[best] < total * 0.1:
            return "注目"
        return best

    def _extract_top_comment(self) -> str:
        clean_comments: list[str] = []
        all_ng_words = set()
        for words in NG_GENRE_DICT.values():
            all_ng_words.update(words)

        for _, text in self.comments:
            cleaned = self.ng_regex.sub('', text).strip()
            if not cleaned:
                continue
            # NGワード単体のコメントは除外
            if cleaned in all_ng_words:
                continue
            clean_comments.append(cleaned)

        if not clean_comments:
            return "リアクション多数"
        return Counter(clean_comments).most_common(1)[0][0][:15]


class RealtimeDetector(ExcitementDetector):
    """Proモード用 — システム時間 (time.time()) 基準"""

    def __init__(self, settings: SettingsManager, **kwargs):
        super().__init__(**kwargs)
        self.settings = settings

    def _current_time(self) -> float:
        return time.time()

    def add_comment(self, text: str, timestamp: float | None = None) -> None:
        super().add_comment(text, time.time())

    def check_excitement_with_limits(self) -> dict | None:
        """保存枠チェック付き"""
        if not self.settings.can_save():
            return {"error": "limit_reached"}
        result = self.check_excitement(time.time())
        if result:
            self.settings.increment_save_count()
        return result


class ArchiveDetector(ExcitementDetector):
    """Freeモード用 — 動画内経過時間 (time_in_seconds) 基準"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.highlights: list[dict] = []

    def _current_time(self) -> float:
        if self.comments:
            return self.comments[-1][0]
        return 0.0

    def add_comment(self, text: str, video_time: float) -> None:
        super().add_comment(text, video_time)

    def scan_and_collect(self, video_time: float) -> dict | None:
        """盛り上がりを検出したら highlights リストに蓄積"""
        result = self.check_excitement(video_time)
        if result:
            result["timestamp"] = video_time
            self.highlights.append(result)
        return result


# ------------------------------------------
# ClipNamer: Proモード用のファイルリネーム
# ------------------------------------------
class ClipNamer:
    def __init__(self):
        self.check_interval = 1.0
        self.timeout = 15.0

    def rename_latest_clip(self, obs_output_dir: str, stream_start_time: float,
                           duration_sec: int, comment_info: dict):
        elapsed = time.time() - stream_start_time
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        s = int(elapsed % 60)
        dur_str = f"{int(duration_sec // 60)}m{int(duration_sec % 60)}s"
        safe_cmt = re.sub(r'[\\/:*?"<>|]', '', comment_info["top_comment"])
        new_filename = f"{h:02}{m:02}{s:02}_{dur_str}_{safe_cmt}_{comment_info['genre']}シーン.mp4"

        threading.Thread(target=self._wait_and_rename,
                         args=(obs_output_dir, new_filename)).start()

    def _wait_and_rename(self, dir_path: str, new_name: str):
        print(f"   [保存処理] ファイル書き込み待機中... ({new_name})")
        start_wait = time.time()
        target_file = None
        last_size = -1

        while time.time() - start_wait < self.timeout:
            try:
                files = [
                    os.path.join(dir_path, f)
                    for f in os.listdir(dir_path)
                    if f.endswith('.mp4') or f.endswith('.mkv')
                ]
            except OSError:
                files = []

            if files:
                latest = max(files, key=os.path.getmtime)
                try:
                    current_size = os.path.getsize(latest)
                    if current_size == last_size and current_size > 0:
                        target_file = latest
                        break
                    last_size = current_size
                except OSError:
                    pass
            time.sleep(self.check_interval)

        if target_file:
            try:
                os.rename(target_file, os.path.join(dir_path, new_name))
                print(f"   ✅ [完了] リネーム成功: {new_name}")
            except Exception as e:
                print(f"   ❌ [エラー] リネーム失敗: {e}")
        else:
            print("   ⚠️ [警告] 保存ファイルが見つかりませんでした (タイムアウト)")


# ==========================================
# 4. モード別メイン処理
# ==========================================

def run_free_mode():
    """【1】Freeモード — アーカイブ解析 & タイムスタンプ抽出"""

    print("\n" + "─" * 50)
    print("  📂 Freeモード: アーカイブ高速解析")
    print("─" * 50)

    # --- URL入力 ---
    while True:
        raw_url = input("\n配信のURL（アーカイブ/VOD）を入力: ").strip()
        if not raw_url:
            continue
        url = clean_url(raw_url)
        if raw_url != url:
            print(f"✨ [自動修正] URLを整形しました:\n   {url}")
        break

    print(f"\n⏳ チャットデータをダウンロード中... ({url})")
    print("   ※ 長時間の配信はデータ量が多いため少々お待ちください\n")

    # --- チャット取得 ---
    try:
        downloader = ChatDownloader()
        chat = downloader.get_chat(url)
    except Exception as e:
        print(f"❌ チャットの取得に失敗しました: {e}")
        print("💡 ヒント: URLが正しいか、配信がアーカイブとして公開されているか確認してください。")
        return

    detector = ArchiveDetector(threshold=10, cooldown=60.0)
    total_comments = 0

    # --- 全コメントをスキャン ---
    try:
        for msg in chat:
            text = msg.get('message') or ""
            if not text:
                continue

            # 動画内の経過時間を取得
            video_time = msg.get('time_in_seconds')
            if video_time is None:
                # time_text から秒数を算出（"1:23:45" 形式）
                time_text = msg.get('time_text', '')
                if time_text:
                    parts = time_text.split(':')
                    try:
                        parts = [int(p) for p in parts]
                        if len(parts) == 3:
                            video_time = parts[0] * 3600 + parts[1] * 60 + parts[2]
                        elif len(parts) == 2:
                            video_time = parts[0] * 60 + parts[1]
                        else:
                            continue
                    except ValueError:
                        continue
                else:
                    continue

            total_comments += 1
            detector.add_comment(text, float(video_time))
            detector.scan_and_collect(float(video_time))

            # 進捗表示（1000件ごと）
            if total_comments % 1000 == 0:
                ts = format_timestamp(float(video_time))
                print(f"   📊 {total_comments:,} 件処理済み ... 現在位置 [{ts}]")

    except KeyboardInterrupt:
        print("\n⚠️ 解析を中断しました。途中までの結果を表示します。")
    except Exception as e:
        print(f"\n⚠️ チャット読み込み中にエラー: {e}")
        print("   途中までの結果を表示します。")

    # --- 結果出力 ---
    highlights = detector.highlights
    print("\n" + "=" * 60)
    print(f"  🎬 解析完了！ (総コメント数: {total_comments:,})")
    print("=" * 60)

    if not highlights:
        print("\n盛り上がりポイントは検出されませんでした。")
        print("💡 ヒント: コメント数が少ない配信では検知されにくい場合があります。")
        return

    print(f"\n🔥 盛り上がりポイント: {len(highlights)} 件検出\n")

    output_lines: list[str] = []
    for i, h in enumerate(highlights, 1):
        ts = format_timestamp(h["timestamp"])
        line = f"[{ts}] {h['genre']}シーン (コメント数:{h['count']}) - {h['top_comment']}"
        print(f"  {i:>3}. {line}")
        output_lines.append(line)

    # --- highlights.txt に書き出し ---
    try:
        with open(HIGHLIGHTS_FILE, "w", encoding="utf-8") as f:
            f.write(f"# AutoClip - ハイライト解析結果\n")
            f.write(f"# URL: {url}\n")
            f.write(f"# 解析日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# 総コメント数: {total_comments:,}\n")
            f.write(f"# 検出数: {len(highlights)}\n")
            f.write("─" * 50 + "\n\n")
            for line in output_lines:
                f.write(line + "\n")

        abs_path = os.path.abspath(HIGHLIGHTS_FILE)
        print(f"\n📄 結果ファイルを保存しました: {abs_path}")
    except Exception as e:
        print(f"\n⚠️ ファイル保存に失敗しました: {e}")


def run_pro_mode(settings: SettingsManager):
    """【2】Proモード — リアルタイムOBS全自動録画"""

    print("\n" + "─" * 50)
    print("  🎮 Proモード: リアルタイムOBS全自動録画")
    print("─" * 50)

    # --- Pro権限チェック ---
    if not settings._is_pro_active():
        remaining = settings.remaining_saves()
        if remaining <= 0:
            print("\n⛔️ 本日の無料保存枠(5回)を使い切りました。")
            print("   Pro版にアップグレードすると無制限で利用できます。")
            return
        print(f"\n⚠️  この機能はPro版限定ですが、無料枠で体験できます。")
        print(f"   本日の残り保存回数: {remaining}/5 回")
        confirm = input("   無料枠を消費して実行しますか？ (y/n): ").strip().lower()
        if confirm != 'y':
            print("   キャンセルしました。")
            return

    # --- OBSライブラリ確認 ---
    if not OBS_AVAILABLE:
        print("\n❌ obsws-python が見つかりません。")
        print("   pip install obsws-python を実行してください。")
        return

    # --- OBS接続 ---
    print("\n[STEP 1] OBS Studioに接続します...")
    print("OBSを起動し、リプレイバッファをONにしてください。")
    obs_pass = input("OBS WebSocketパスワードを入力 (未設定ならそのままEnter): ").strip()

    try:
        obs_client = ReqClient(host='localhost', port=4455, password=obs_pass)
        obs_record_dir = obs_client.get_record_directory().record_directory
        print(f"✅ OBS接続成功！ 保存先: {obs_record_dir}")
    except Exception as e:
        print(f"❌ OBS接続エラー: {e}")
        print("OBSの設定を確認してください (ツール -> WebSocketサーバー設定)")
        return

    detector = RealtimeDetector(settings, threshold=10, cooldown=60.0)
    renamer = ClipNamer()

    # --- URL入力 ---
    while True:
        raw_url = input("\n[STEP 2] YouTube LiveのURLを入力: ").strip()
        if not raw_url:
            continue
        url = clean_url(raw_url)
        if raw_url != url:
            print(f"✨ [自動修正] URLを整形しました:\n   {url}")
        if "twitch.tv" in url and not settings._is_pro_active():
            print("⚠️ Free枠ではTwitchは未対応です。")
            continue
        break

    buffer_len_str = input("[STEP 3] リプレイバッファの長さ(秒) [デフォルト180]: ").strip()
    buffer_len = int(buffer_len_str) if buffer_len_str else 180

    # --- 監視ループ ---
    print(f"\n🚀 監視を開始します: {url}")
    print("停止するには Ctrl + C を押してください\n")

    stream_start_time = time.time()

    try:
        downloader = ChatDownloader()
        chat = downloader.get_chat(url)

        for msg in chat:
            text = msg.get('message') or ""
            if not text:
                continue

            detector.add_comment(text)
            result = detector.check_excitement_with_limits()

            if result is None:
                continue

            if "error" in result:
                print("\n⛔️ 本日の無料保存枠(5回)を使い切りました。終了します。")
                break

            print(f"\n🔥 [検知] {result['genre']}シーン "
                  f"(コメント数:{result['count']}) - {result['top_comment']}")

            # OBS保存実行
            try:
                obs_client.save_replay_buffer()
            except Exception as e:
                print(f"   ⚠️ リプレイバッファ保存エラー: {e}")
                continue

            renamer.rename_latest_clip(obs_record_dir, stream_start_time,
                                       buffer_len, result)

            time.sleep(detector.cooldown_time)
            print(f"❄️ クールダウン完了 ({detector.cooldown_time}秒)\n")

    except KeyboardInterrupt:
        print("\n👋 監視を停止しました。")
    except Exception as e:
        print(f"\n❌ エラーが発生しました: {e}")
        print("💡 ヒント: 配信が終了しているか、URLが間違っている可能性があります。"
              "現在ライブ中の配信でお試しください。")


# ==========================================
# 5. エントリーポイント
# ==========================================

def main():
    print("\n" + "=" * 55)
    print("   🎬 AutoClip v5 — YouTube盛り上がり検知ツール")
    print("=" * 55)
    print()
    print("  【1】 Free  — アーカイブ高速解析（タイムスタンプ抽出）")
    print("  【2】 Pro   — リアルタイムOBS全自動録画")
    print("  【q】 終了")
    print()

    while True:
        choice = input("モードを選択してください [1/2/q]: ").strip().lower()

        if choice == '1':
            run_free_mode()
            break
        elif choice == '2':
            settings = SettingsManager()
            run_pro_mode(settings)
            break
        elif choice in ('q', 'quit', 'exit'):
            print("👋 終了します。")
            break
        else:
            print("⚠️ 1, 2, または q を入力してください。")


if __name__ == "__main__":
    main()
