from __future__ import annotations  # Python 3.9以下でも新しい型ヒントを使えるようにする
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk
import threading
import time
import json
import re
import os
import sys
from collections import deque
from datetime import datetime
from typing import Optional, List, Dict, Any

# 外部ライブラリのインポート（インストールされていない場合の対策）
try:
    from chat_downloader import ChatDownloader
    from obsws_python import ReqClient
except ImportError:
    # GUI起動後にエラー表示するため、ここではパス
    pass

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
# 2. クラス定義
# ==========================================

class ExcitementDetector:
    """コメントの盛り上がり判定と、トップコメント・ジャンルの抽出を行うクラス"""
    def __init__(self, settings_manager):
        self.comments = deque()  # (timestamp, text)
        self.settings = settings_manager
        self.threshold_count = 10
        self.cooldown_time = 60
        self.last_trigger_time = 0
        self.ng_regex = re.compile(r'[\U00010000-\U0010ffff]|[\u2000-\u2fff]|[:;][a-zA-Z0-9_]+[:;]|\(.*\)|（.*）')

    def add_comment(self, text: str, timestamp: float) -> None:
        now = time.time()
        self.comments.append((now, text))
        # 30秒より前のコメントを削除
        while self.comments and self.comments[0][0] < now - 30:
            self.comments.popleft()

    def check_excitement(self) -> dict | None:
        now = time.time()
        if now - self.last_trigger_time < self.cooldown_time:
            return None
        
        if len(self.comments) >= self.threshold_count:
            # 1日の保存制限チェック (Free版のみ)
            if not self.settings.can_save():
                return {"error": "limit_reached"}

            genre = self._determine_genre()
            top_comment = self._extract_top_comment()
            
            self.last_trigger_time = now
            self.settings.increment_save_count()
            
            return {
                "genre": genre,
                "top_comment": top_comment,
                "count": len(self.comments)
            }
        return None

    def _determine_genre(self) -> str:
        counts = {k: 0 for k in NG_GENRE_DICT.keys()}
        total_comments = len(self.comments)
        
        for _, text in self.comments:
            for genre, words in NG_GENRE_DICT.items():
                if any(w in text for w in words):
                    counts[genre] += 1
        
        # 最も多いジャンルを探す
        best_genre = max(counts, key=counts.get)
        if counts[best_genre] < total_comments * 0.1: # 全体の10%未満なら
            return "注目"
        return best_genre

    def _extract_top_comment(self) -> str:
        # NGワードのみ、または絵文字のみのコメントを除外して集計
        clean_comments = []
        for _, text in self.comments:
            # 絵文字削除
            clean_text = self.ng_regex.sub('', text).strip()
            
            # NGワード判定
            is_ng_only = False
            for words in NG_GENRE_DICT.values():
                if any(clean_text == w for w in words) or clean_text == "":
                    is_ng_only = True
                    break
            
            if not is_ng_only:
                clean_comments.append(clean_text)
        
        if not clean_comments:
            return "リアクション多数"
            
        # 最頻出（同じコメントがあればそれ）、なければ一番長いもの
        from collections import Counter
        most_common = Counter(clean_comments).most_common(1)
        
        result = most_common[0][0]
        return result[:15] # 最大15文字でカット


class ClipNamer:
    """ファイルのリネームと保存管理を行うクラス"""
    def __init__(self, check_interval=0.5, timeout=10.0):
        self.check_interval = check_interval
        self.timeout = timeout

    def rename_latest_clip(self, obs_output_dir, stream_start_time, duration_sec, comment_info):
        # 配信開始からの経過時間 (HHMMSS)
        elapsed = time.time() - stream_start_time
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        s = int(elapsed % 60)
        time_str = f"{h:02}{m:02}{s:02}"
        
        # クリップ長表記 (XmYs)
        dur_m = int(duration_sec // 60)
        dur_s = int(duration_sec % 60)
        dur_str = f"{dur_m}m{dur_s}s"
        
        # ファイル名組み立て
        safe_comment = re.sub(r'[\\/:*?"<>|]', '', comment_info["top_comment"])
        safe_genre = comment_info["genre"]
        new_filename = f"{time_str}_{dur_str}_{safe_comment}_{safe_genre}シーン.mp4"
        
        # 保存待機とリネーム実行
        threading.Thread(target=self._wait_and_rename, 
                         args=(obs_output_dir, new_filename)).start()
        return new_filename

    def _wait_and_rename(self, dir_path, new_name):
        # 最新ファイルを探すループ
        start_wait = time.time()
        target_file = None
        last_size = -1
        
        while time.time() - start_wait < self.timeout:
            files = [os.path.join(dir_path, f) for f in os.listdir(dir_path) if f.endswith('.mp4') or f.endswith('.mkv')]
            if not files:
                time.sleep(self.check_interval)
                continue
                
            latest = max(files, key=os.path.getmtime)
            
            try:
                current_size = os.path.getsize(latest)
                if current_size == last_size and current_size > 0:
                    # 書き込み完了とみなす
                    target_file = latest
                    break
                last_size = current_size
            except OSError:
                pass
                
            time.sleep(self.check_interval)
            
        if target_file:
            try:
                new_path = os.path.join(dir_path, new_name)
                os.rename(target_file, new_path)
                print(f"Rename Success: {new_name}")
            except Exception as e:
                print(f"Rename Failed: {e}")


class SettingsManager:
    """Free版/Pro版の管理と設定保存"""
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
            except:
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

    def can_save(self):
        self._check_daily_reset()
        if self._is_pro_active():
            return True
        return self.data["daily_saves"] < 5

    def increment_save_count(self):
        self.data["daily_saves"] += 1
        self.save()

    def _is_pro_active(self):
        if self.data["is_pro"]: return True
        if self.data["holiday_pass_expiry"]:
            expiry = datetime.fromisoformat(self.data["holiday_pass_expiry"])
            if datetime.now() < expiry:
                return True
        return False
        
    def activate_holiday_pass(self):
        from datetime import timedelta
        self.data["holiday_pass_expiry"] = (datetime.now() + timedelta(days=14)).isoformat()
        self.data["tutorial_done"] = True
        self.save()

# ==========================================
# 3. メインアプリケーション (GUI)
# ==========================================

class AutoClipApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("AutoClip Pro (Mac Fixed)")
        self.root.geometry("600x650")
        
        # Mac特有のブラックスクリーン対策
        self.root.update_idletasks()
        
        self.settings = SettingsManager()
        self.detector = ExcitementDetector(self.settings)
        self.renamer = ClipNamer()
        
        self.is_monitoring = False
        self.monitor_thread = None
        self.stop_event = threading.Event()
        self.obs_client = None
        self.obs_record_dir = os.path.expanduser("~/Movies") # デフォルト
        self.stream_start_time = 0

        self._setup_ui()

    def _setup_ui(self):
        # スタイル設定
        style = ttk.Style()
        style.configure("TButton", font=("Hiragino Maru Gothic ProN", 12))
        style.configure("TLabel", font=("Hiragino Maru Gothic ProN", 10))

        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. URL入力
        ttk.Label(main_frame, text="YouTube Live URL:").pack(anchor="w")
        self.url_var = tk.StringVar()
        ttk.Entry(main_frame, textvariable=self.url_var).pack(fill=tk.X, pady=5)

        # 2. OBS設定
        obs_frame = ttk.LabelFrame(main_frame, text="OBS WebSocket 設定", padding=5)
        obs_frame.pack(fill=tk.X, pady=10)
        
        ttk.Label(obs_frame, text="Port (例: 4455):").grid(row=0, column=0, padx=5)
        self.port_var = tk.StringVar(value="4455")
        ttk.Entry(obs_frame, textvariable=self.port_var, width=10).grid(row=0, column=1)
        
        ttk.Label(obs_frame, text="Password:").grid(row=0, column=2, padx=5)
        self.pwd_var = tk.StringVar()
        ttk.Entry(obs_frame, textvariable=self.pwd_var, show="*", width=15).grid(row=0, column=3)
        
        # 3. リプレイバッファ秒数
        ttk.Label(main_frame, text="リプレイバッファ長 (秒):").pack(anchor="w")
        self.buffer_var = tk.StringVar(value="180")
        ttk.Entry(main_frame, textvariable=self.buffer_var, width=10).pack(anchor="w", pady=5)

        # 4. ボタンエリア
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(pady=10)
        
        self.start_btn = ttk.Button(btn_frame, text="監視開始", command=self.toggle_monitoring)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(btn_frame, text="保存先を開く", command=self.open_folder).pack(side=tk.LEFT, padx=5)
        
        # ダミー登録ボタン（デバッグ用）
        ttk.Button(main_frame, text="[DEBUG] チュートリアル完了", command=self.mock_tutorial_complete).pack(anchor="e", pady=5)

        # 5. ログ表示 (Mac対策: ScrolledTextではなくFrame+Textで構成)
        log_frame = ttk.LabelFrame(main_frame, text="実行ログ", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True)
        
        self.log_area = scrolledtext.ScrolledText(log_frame, height=10, state='disabled')
        self.log_area.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        self.root.after(0, self._log_thread_safe, message)

    def _log_thread_safe(self, message):
        self.log_area.config(state='normal')
        self.log_area.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n")
        self.log_area.see(tk.END)
        self.log_area.config(state='disabled')

    def open_folder(self):
        try:
            os.system(f"open '{self.obs_record_dir}'")
        except:
            self.log("フォルダを開けませんでした")

    def toggle_monitoring(self):
        if not self.is_monitoring:
            self.start_monitoring()
        else:
            self.stop_monitoring()

    def start_monitoring(self):
        url = self.url_var.get()
        
        # Twitchブロック機能 (Free版)
        if "twitch.tv" in url and not self.settings._is_pro_active():
            messagebox.showinfo("Pro版限定", "Twitch対応はHoliday PassまたはPro版が必要です。\nチュートリアルを完了してPassをゲットしよう！")
            return

        # OBS接続テスト
        try:
            self.obs_client = ReqClient(host='localhost', port=int(self.port_var.get()), password=self.pwd_var.get())
            resp = self.obs_client.get_record_directory()
            self.obs_record_dir = resp.record_directory
            self.log(f"OBS接続成功: {self.obs_record_dir}")
        except Exception as e:
            messagebox.showerror("OBSエラー", f"OBSに接続できませんでした。\n{e}")
            return

        self.is_monitoring = True
        self.stop_event.clear()
        self.start_btn.config(text="監視停止")
        self.stream_start_time = time.time()
        
        self.monitor_thread = threading.Thread(target=self._monitor_loop, args=(url,), daemon=True)
        self.monitor_thread.start()
        self.log("監視を開始しました...")

    def stop_monitoring(self):
        self.is_monitoring = False
        self.stop_event.set()
        self.start_btn.config(text="監視開始")
        self.log("監視を停止しました")

    def _monitor_loop(self, url):
        try:
            downloader = ChatDownloader()
            chat = downloader.get_chat(url)
            
            for msg in chat:
                if self.stop_event.is_set(): break
                
                # メッセージ処理
                text = msg.get('message')
                if text:
                    self.detector.add_comment(text, time.time())
                    
                # 盛り上がり判定
                result = self.detector.check_excitement()
                
                # エラー（回数制限）チェック
                if result and "error" in result:
                    self.log("【制限】本日の無料保存枠(5回)を使い切りました")
                    self.root.after(0, self.stop_monitoring)
                    self.root.after(0, lambda: messagebox.showinfo("制限到達", "本日の無料枠終了！\n明日また使うか、Pro版へアップグレードしてください。"))
                    break
                
                # 保存トリガー発火
                if result:
                    self.log(f"★検知: {result['genre']} (数:{result['count']}) - {result['top_comment']}")
                    self._trigger_obs_save(result)
                    time.sleep(self.detector.cooldown_time) # クールダウン
                    
        except Exception as e:
            self.log(f"監視エラー: {e}")
            self.root.after(0, self.stop_monitoring)

    def _trigger_obs_save(self, comment_info):
        try:
            self.obs_client.save_replay_buffer()
            self.log("OBS保存リクエスト送信")
            
            # リネーム処理開始
            buffer_len = int(self.buffer_var.get())
            self.renamer.rename_latest_clip(
                self.obs_record_dir, 
                self.stream_start_time, 
                buffer_len, 
                comment_info
            )
        except Exception as e:
            self.log(f"保存失敗: {e}")

    # ==========================
    # ダミー画面ロジック (Pro体験)
    # ==========================
    def mock_tutorial_complete(self):
        if self.settings.data["tutorial_done"]:
            self.log("既にチュートリアル済みです")
            return
            
        # 1. 成功ポップアップ
        messagebox.showinfo("おめでとう！", "初めてのクリップを作成しました！\n報酬として「Holiday Pass (14日間Pro権限)」をプレゼントします！")
        
        # 2. 登録ウィンドウ（ダミー）
        reg_win = tk.Toplevel(self.root)
        reg_win.title("アカウント登録 (ダミー)")
        reg_win.geometry("300x200")
        ttk.Label(reg_win, text="メールアドレス:").pack(pady=5)
        ttk.Entry(reg_win).pack()
        ttk.Label(reg_win, text="カード情報 (Stripe Mock):").pack(pady=5)
        ttk.Entry(reg_win).pack()
        
        def on_reg():
            reg_win.destroy()
            self._mock_survey()
            
        ttk.Button(reg_win, text="登録してPassを受け取る", command=on_reg).pack(pady=20)

    def _mock_survey(self):
        # 3. アンケート（ダミー）
        sur_win = tk.Toplevel(self.root)
        sur_win.title("初期アンケート")
        sur_win.geometry("300x200")
        ttk.Label(sur_win, text="普段の配信サイトは？").pack(pady=10)
        ttk.Combobox(sur_win, values=["YouTube", "Twitch", "Both"]).pack()
        
        def on_finish():
            sur_win.destroy()
            self.settings.activate_holiday_pass()
            self.log("★ Holiday Pass が有効になりました！Twitch対応解禁！")
            messagebox.showinfo("完了", "設定完了！\n14日間、Pro機能が使い放題です。")
            
        ttk.Button(sur_win, text="回答して開始", command=on_finish).pack(pady=20)


# ==========================================
# 4. アプリ起動
# ==========================================
if __name__ == "__main__":
    root = tk.Tk()
    app = AutoClipApp(root)
    root.mainloop()