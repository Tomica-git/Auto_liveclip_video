from __future__ import annotations
import threading
import time
import json
import re
import os
import sys
from collections import deque
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# 外部ライブラリのインポート
try:
    from chat_downloader import ChatDownloader
    from obsws_python import ReqClient
except ImportError:
    print("ライブラリが見つかりません。pip install -r requirements.txt を実行してください。")
    sys.exit(1)

# ==========================================
# 1. 設定・定数定義
# ==========================================
SETTINGS_FILE = "autoclip_settings.json"
NG_GENRE_DICT = {
    "爆笑": ["w", "ww", "www", "草", "竹", "腹痛い", "ワロタ", "lol", "lmao", "笑"],
    "称賛": ["神", "うっま", "うまい", "8888", "すごい", "nice", "god", "最高", "かっこいい"],
    "失敗": ["あ", "ああ", "PON", "トロール", "やらかした", "ドンマイ", "F", "rip", "ミス"],
    "困惑": ["！？", "？？？", "は？", "ま？", "?", "what", "why", "bug", "バグ"],
    "Twitch": ["LUL", "Pog", "Kappa", "KEKW", "monkaS", "Pepega", "Sadge", "EZ"]
}

# ==========================================
# 2. ロジッククラス
# ==========================================

class SettingsManager:
    def __init__(self):
        self.data = {
            "is_pro": False, 
            "holiday_pass_expiry": None,
            "daily_saves": 0, 
            "last_save_date": "",
            "tutorial_done": False
        }
        self.load()

    def load(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    self.data.update(json.load(f))
            except: pass
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

    def can_save(self):
        self._check_daily_reset()
        if self._is_pro_active(): return True
        return self.data["daily_saves"] < 5

    def increment_save_count(self):
        self.data["daily_saves"] += 1
        self.save()

    def _is_pro_active(self):
        if self.data["is_pro"]: return True
        if self.data["holiday_pass_expiry"]:
            expiry = datetime.fromisoformat(self.data["holiday_pass_expiry"])
            if datetime.now() < expiry: return True
        return False

class ExcitementDetector:
    def __init__(self, settings_manager):
        self.comments = deque()
        self.settings = settings_manager
        self.threshold_count = 10
        self.cooldown_time = 60
        self.last_trigger_time = 0
        self.ng_regex = re.compile(r'[\U00010000-\U0010ffff]|[\u2000-\u2fff]|[:;][a-zA-Z0-9_]+[:;]|\(.*\)|（.*）')

    def add_comment(self, text: str, timestamp: float) -> None:
        now = time.time()
        self.comments.append((now, text))
        while self.comments and self.comments[0][0] < now - 30:
            self.comments.popleft()

    def check_excitement(self) -> dict | None:
        now = time.time()
        if now - self.last_trigger_time < self.cooldown_time: return None
        if len(self.comments) >= self.threshold_count:
            if not self.settings.can_save(): return {"error": "limit_reached"}
            
            genre = self._determine_genre()
            top_comment = self._extract_top_comment()
            self.last_trigger_time = now
            self.settings.increment_save_count()
            return {"genre": genre, "top_comment": top_comment, "count": len(self.comments)}
        return None

    def _determine_genre(self) -> str:
        counts = {k: 0 for k in NG_GENRE_DICT.keys()}
        total_comments = len(self.comments)
        for _, text in self.comments:
            for genre, words in NG_GENRE_DICT.items():
                if any(w in text for w in words): counts[genre] += 1
        best_genre = max(counts, key=counts.get)
        if counts[best_genre] < total_comments * 0.1: return "注目"
        return best_genre

    def _extract_top_comment(self) -> str:
        clean_comments = []
        for _, text in self.comments:
            clean_text = self.ng_regex.sub('', text).strip()
            is_ng_only = False
            for words in NG_GENRE_DICT.values():
                if any(clean_text == w for w in words) or clean_text == "":
                    is_ng_only = True; break
            if not is_ng_only: clean_comments.append(clean_text)
        
        if not clean_comments: return "リアクション多数"
        from collections import Counter
        return Counter(clean_comments).most_common(1)[0][0][:15]

class ClipNamer:
    def __init__(self):
        self.check_interval = 1.0 # コンソール版は少しゆっくりチェック
        self.timeout = 15.0

    def rename_latest_clip(self, obs_output_dir, stream_start_time, duration_sec, comment_info):
        elapsed = time.time() - stream_start_time
        h, m, s = int(elapsed // 3600), int((elapsed % 3600) // 60), int(elapsed % 60)
        dur_str = f"{int(duration_sec // 60)}m{int(duration_sec % 60)}s"
        safe_cmt = re.sub(r'[\\/:*?"<>|]', '', comment_info["top_comment"])
        new_filename = f"{h:02}{m:02}{s:02}_{dur_str}_{safe_cmt}_{comment_info['genre']}シーン.mp4"
        
        threading.Thread(target=self._wait_and_rename, args=(obs_output_dir, new_filename)).start()

    def _wait_and_rename(self, dir_path, new_name):
        print(f"   [保存処理] ファイル書き込み待機中... ({new_name})")
        start_wait = time.time()
        target_file = None
        last_size = -1
        
        while time.time() - start_wait < self.timeout:
            files = [os.path.join(dir_path, f) for f in os.listdir(dir_path) if f.endswith('.mp4') or f.endswith('.mkv')]
            if files:
                latest = max(files, key=os.path.getmtime)
                try:
                    current_size = os.path.getsize(latest)
                    if current_size == last_size and current_size > 0:
                        target_file = latest; break
                    last_size = current_size
                except: pass
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
# 3. メイン処理 (コンソール版)
# ==========================================
def clean_url(raw_url: str) -> str:
    """URLから不要なトラッキングパラメータ（siなど）を削除する"""
    parsed = urlparse(raw_url)
    query = parse_qs(parsed.query)
    
    # 消したいゴミパラメータのリスト
    trash_params = ['si', 'feature', 'ab_channel']
    for param in trash_params:
        if param in query:
            del query[param]
            
    new_query = urlencode(query, doseq=True)
    clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
    return clean_url

def main():
    print("\n" + "="*50)
    print("   AutoClip Pro (Console Mode) - Mac Optimized")
    print("="*50)

    # 1. OBS接続確認
    print("\n[STEP 1] OBS Studioに接続します...")
    print("OBSを起動し、リプレイバッファをONにしてください。")
    obs_pass = input("OBS WebSocketパスワードを入力 (設定してない場合はそのままEnter): ")
    
    obs_client = None
    obs_record_dir = ""
    try:
        obs_client = ReqClient(host='localhost', port=4455, password=obs_pass)
        obs_record_dir = obs_client.get_record_directory().record_directory
        print(f"✅ OBS接続成功！ 保存先: {obs_record_dir}")
    except Exception as e:
        print(f"❌ OBS接続エラー: {e}")
        print("OBSの設定を確認してください (ツール -> WebSocketサーバー設定)")
        return

    # 2. 初期設定
    settings = SettingsManager()
    detector = ExcitementDetector(settings)
    renamer = ClipNamer()
    
    # 3. URL入力とバリデーション
    while True:
        raw_url = input("\n[STEP 2] YouTube LiveのURLを入力: ").strip()
        
        # --- ここでゴミを自動削除！ ---
        url = clean_url(raw_url)
        if raw_url != url:
            print(f"✨ [自動修正] URLのゴミを削除しました: {url}")
            
        if "twitch.tv" in url and not settings._is_pro_active():
            print("⚠️ Free版ではTwitchは未対応です。")
            continue
        if url: break

    buffer_len = input("[STEP 3] リプレイバッファの長さ(秒) [デフォルト180]: ").strip()
    if not buffer_len: buffer_len = 180
    buffer_len = int(buffer_len)

    # 4. 監視ループ開始
    print(f"\n🚀 監視を開始します: {url}")
    print("停止するには 'Ctrl + C' を押してください\n")
    
    stream_start_time = time.time()
    
    try:
        downloader = ChatDownloader()
        chat = downloader.get_chat(url)
        
        for msg in chat:
            text = msg.get('message') or ""
            author = msg.get('author', {}).get('name', 'Unknown')
            timestamp = datetime.now().strftime('%H:%M:%S')
            
            # コメントが取得できているか確認したい場合は下の行の「#」を消してください
            # print(f"[{timestamp}] {author}: {text}") 

            if text:
                detector.add_comment(text, time.time())
                
            result = detector.check_excitement()
            
            if result:
                if "error" in result:
                    print("\n⛔️ 本日の無料保存枠(5回)を使い切りました。終了します。")
                    break
                
                print(f"\n🔥 [検知] {result['genre']} (コメント数:{result['count']}) - {result['top_comment']}")
                
                # OBS保存実行
                obs_client.save_replay_buffer()
                renamer.rename_latest_clip(obs_record_dir, stream_start_time, buffer_len, result)
                
                time.sleep(detector.cooldown_time)
                print(f"❄️ クールダウン中... ({detector.cooldown_time}秒)\n")
                
    except KeyboardInterrupt:
        print("\n👋 監視を停止しました。")
    except Exception as e:
        print(f"\n❌ エラーが発生しました: {e}")
        print("💡 ヒント: 配信が終了しているか、URLが間違っている可能性があります。現在ライブ中の配信でお試しください。")

if __name__ == "__main__":
    main()