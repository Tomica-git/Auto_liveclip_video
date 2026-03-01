from __future__ import annotations
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoClip Pro — YouTube/Twitch Live 自動クリッパー (プロトタイプ)

盛り上がりを検知してOBSのリプレイバッファを自動保存・リネームする。
SaaSモデル: Free版（YouTube, 5回/日）/ Holiday Pass（YouTube+Twitch, 無制限, 14日間）
"""

import os
import re
import json
import time
import glob
import queue
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime, timedelta
from collections import deque, Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
SETTINGS_FILE = "autoclip_settings.json"
FREE_DAILY_LIMIT = 5
HOLIDAY_PASS_DAYS = 14
EXTENSION_DAYS = 7
EXTENSION_THRESHOLD_DAYS = 7

# ---------------------------------------------------------------------------
# ExcitementDetector — 盛り上がり判定 + ジャンル分類 + トップコメント抽出
# ---------------------------------------------------------------------------
class ExcitementDetector:
    """直近コメントを分析し、盛り上がりを判定。ジャンルとトップコメントを抽出する。"""

    # NGワードジャンル辞書（キー=ジャンル名, 値=キーワードリスト）
    NG_GENRE_DICT: dict[str, list[str]] = {
        "爆笑": [
            "w", "ww", "www", "wwww", "wwwww", "wwwwww",
            "草", "竹", "腹痛い", "ワロタ", "lol", "lmao", "笑",
        ],
        "称賛": [
            "神", "うっま", "うまい", "うめー", "うめえ", "プロ",
            "8888", "88888", "888888", "すごい", "すげー", "すげえ",
            "ナイス", "GG", "gg", "nice", "god", "最高", "かっこいい",
        ],
        "失敗": [
            "あ", "ああ", "あー", "PON", "トロール", "やらかした",
            "ドンマイ", "F", "rip", "dead", "ミス", "乙", "下手",
        ],
        "困惑": [
            "！？", "？？？", "は？", "ま？", "？", "?", "??", "???",
            "え", "えぇ", "なに", "what", "why", "how", "バグ",
        ],
        "Twitch文化": [
            "PogChamp", "Pog", "KEKW", "LUL", "OMEGALUL",
            "MonkaS", "Pepega", "Sadge", "Copium", "kekw", "pog",
            "monkaS", "monkas",
        ],
    }

    # 全NGワードをフラットなセットに（小文字化して判定用）
    _ALL_NG_LOWER: set[str] = set()
    for _words in NG_GENRE_DICT.values():
        for _w in _words:
            _ALL_NG_LOWER.add(_w.lower())

    # 絵文字・顔文字除去用の正規表現パターン
    # Unicode絵文字
    RE_EMOJI = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # 表情
        "\U0001F300-\U0001F5FF"  # 記号・ピクトグラフ
        "\U0001F680-\U0001F6FF"  # 交通・地図
        "\U0001F1E0-\U0001F1FF"  # 国旗
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"  # 補助絵文字
        "\U0001FA00-\U0001FA6F"
        "\U0001FA70-\U0001FAFF"
        "\U00002600-\U000026FF"
        "\U0000FE00-\U0000FE0F"
        "\U0000200D"
        "]+",
        flags=re.UNICODE,
    )
    # 顔文字パターン: (^^), (泣), (笑), :-), ;-) 等
    RE_KAOMOJI = re.compile(
        r"[\(（][\w\^\;\:\-\'\*\+\>\<\=ﾟдωﾉ°・。゜]{1,10}[\)）]"
        r"|[:;][\-']?[)(DPpOo\]\[|/\\]"
    )
    # Twitchエモート（大文字始まり英数字、5文字以上のCamelCase等）
    RE_TWITCH_EMOTE = re.compile(r"\b[A-Z][a-zA-Z0-9]{4,}\b")
    # 「w」の連続（1文字以上）
    RE_W_CHAIN = re.compile(r"^[wWｗＷ]+$")
    # 「8」の連続（拍手）
    RE_EIGHT_CHAIN = re.compile(r"^[8８]+$")

    def __init__(self, window_sec: int = 30, threshold: int = 10,
                 cooldown_sec: int = 60, genre_min_ratio: float = 0.10):
        """
        Args:
            window_sec: 分析ウィンドウ（秒）
            threshold: 盛り上がり判定に必要な最低コメント数
            cooldown_sec: 連続検知を防ぐクールダウン（秒）
            genre_min_ratio: ジャンル確定に必要な最低比率（これ未満は「注目」）
        """
        self.window_sec = window_sec
        self.threshold = threshold
        self.cooldown_sec = cooldown_sec
        self.genre_min_ratio = genre_min_ratio
        self.comments: deque = deque()          # (timestamp, text) のペア
        self.last_trigger_time: float = 0.0     # 最後にトリガーした時刻

    def add_comment(self, text: str, timestamp: float | None = None) -> None:
        """コメントをウィンドウに追加し、古いものを除去する。"""
        ts = timestamp or time.time()
        self.comments.append((ts, text))
        cutoff = ts - self.window_sec
        while self.comments and self.comments[0][0] < cutoff:
            self.comments.popleft()

    def check_excitement(self) -> bool:
        """盛り上がりを判定する。閾値とクールダウンで制御。"""
        if len(self.comments) < self.threshold:
            return False
        now = time.time()
        if now - self.last_trigger_time < self.cooldown_sec:
            return False
        self.last_trigger_time = now
        return True

    def classify_genre(self) -> str:
        """直近コメントからNGワードジャンルを分類し、シーン名を返す。"""
        genre_counts: Counter = Counter()
        total = len(self.comments)
        if total == 0:
            return "注目シーン"

        for _, text in self.comments:
            tokens = self._tokenize(text)
            for token in tokens:
                lower = token.lower()
                for genre, keywords in self.NG_GENRE_DICT.items():
                    if any(lower == kw.lower() for kw in keywords):
                        genre_counts[genre] += 1
                        break  # 1トークンは1ジャンルにのみカウント

        if not genre_counts:
            return "注目シーン"

        top_genre, top_count = genre_counts.most_common(1)[0]
        if top_count / total < self.genre_min_ratio:
            return "注目シーン"
        return f"{top_genre}シーン"

    def extract_top_comment(self) -> str:
        """NGワードのみのコメントを除外し、最頻出の意味あるテキストを返す。"""
        meaningful: list[str] = []
        for _, text in self.comments:
            cleaned = self._clean_text(text)
            if not cleaned:
                continue
            # コメント内の全トークンがNGワードだけか判定
            tokens = self._tokenize(cleaned)
            if tokens and all(t.lower() in self._ALL_NG_LOWER for t in tokens):
                continue  # NGワードのみ → 除外
            meaningful.append(cleaned)

        if not meaningful:
            return "リアクション多数"

        counts = Counter(meaningful)
        top_text, _ = counts.most_common(1)[0]
        # 最大15文字に切り詰め
        if len(top_text) > 15:
            top_text = top_text[:15]
        return top_text

    # --- 内部ヘルパー ---

    def _clean_text(self, text: str) -> str:
        """絵文字・顔文字・Twitchエモート・記号を除去し、サニタイズしたテキストを返す。"""
        t = self.RE_EMOJI.sub("", text)
        t = self.RE_KAOMOJI.sub("", t)
        t = t.strip()
        # 空白のみになった場合
        if not t:
            return ""
        return t

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """テキストを空白区切りでトークン化。日本語の場合は1コメント=1トークン扱い。"""
        parts = text.split()
        if len(parts) <= 1:
            return [text.strip()] if text.strip() else []
        return [p for p in parts if p]


# ---------------------------------------------------------------------------
# ClipNamer — ファイル命名 + サイズ安定化待ち + リネーム
# ---------------------------------------------------------------------------
class ClipNamer:
    """ファイル名の生成とリネーム処理を行う。"""

    # OSで禁止されるファイル名文字
    RE_FORBIDDEN = re.compile(r'[\\/:*?"<>|]')

    def __init__(self, check_interval: float = 2.0, max_timeout: float = 60.0):
        """
        Args:
            check_interval: 書き込み完了チェックの間隔（秒）
            max_timeout: 最大待機タイムアウト（秒）
        """
        self.check_interval = check_interval
        self.max_timeout = max_timeout

    def generate_filename(self, elapsed_seconds: float, replay_duration: int,
                          top_comment: str, genre: str) -> str:
        """
        HHMMSS_XmYs_△△_〇〇シーン.mp4 形式のファイル名を生成する。

        Args:
            elapsed_seconds: 配信開始からの経過秒数
            replay_duration: OBSリプレイバッファ長（秒）
            top_comment: トップコメント（△△）
            genre: ジャンル名（〇〇シーン）
        """
        # HHMMSS
        total_sec = int(elapsed_seconds)
        hh = total_sec // 3600
        mm = (total_sec % 3600) // 60
        ss = total_sec % 60
        hhmmss = f"{hh:02d}{mm:02d}{ss:02d}"

        # XmYs
        r_min = replay_duration // 60
        r_sec = replay_duration % 60
        xmys = f"{r_min}m{r_sec}s"

        # △△（サニタイズ）
        sanitized_comment = self._sanitize(top_comment)
        if not sanitized_comment:
            sanitized_comment = "リアクション多数"

        # 〇〇シーン（サニタイズ）
        sanitized_genre = self._sanitize(genre)

        filename = f"{hhmmss}_{xmys}_{sanitized_comment}_{sanitized_genre}.mp4"
        return filename

    def wait_and_rename(self, output_dir: str, new_filename: str,
                        log_callback=None) -> str | None:
        """
        出力ディレクトリ内の最新.mp4ファイルの書き込み完了を待ち、リネームする。

        Args:
            output_dir: OBSの出力ディレクトリ
            new_filename: リネーム後のファイル名
            log_callback: ログ出力用コールバック

        Returns:
            リネーム後のフルパス（成功時）またはNone（失敗時）
        """
        start_time = time.time()
        prev_size = -1

        while time.time() - start_time < self.max_timeout:
            # 最新の.mp4ファイルを検出
            latest = self._find_latest_mp4(output_dir)
            if latest is None:
                if log_callback:
                    log_callback("[ClipNamer] mp4ファイル待機中...")
                time.sleep(self.check_interval)
                continue

            current_size = os.path.getsize(latest)

            if current_size > 0 and current_size == prev_size:
                # サイズが安定 → 書き込み完了
                new_path = os.path.join(output_dir, new_filename)
                # 同名ファイルが存在する場合は連番を付与
                new_path = self._unique_path(new_path)
                try:
                    os.rename(latest, new_path)
                    if log_callback:
                        log_callback(f"[ClipNamer] リネーム完了: {os.path.basename(new_path)}")
                    return new_path
                except OSError as e:
                    if log_callback:
                        log_callback(f"[ClipNamer] リネームエラー: {e}")
                    return None

            prev_size = current_size
            if log_callback:
                log_callback(f"[ClipNamer] 書き込み確認中... ({current_size} bytes)")
            time.sleep(self.check_interval)

        # タイムアウト
        if log_callback:
            log_callback("[ClipNamer] タイムアウト: ファイル書き込みが完了しませんでした")
        return None

    def _sanitize(self, text: str) -> str:
        """ファイル名に使えない文字を除去する。"""
        return self.RE_FORBIDDEN.sub("", text).strip()

    @staticmethod
    def _find_latest_mp4(directory: str) -> str | None:
        """ディレクトリ内の最新のmp4ファイルを検出する。"""
        pattern = os.path.join(directory, "*.mp4")
        files = glob.glob(pattern)
        if not files:
            # .mkv もチェック
            pattern_mkv = os.path.join(directory, "*.mkv")
            files = glob.glob(pattern_mkv)
        if not files:
            return None
        return max(files, key=os.path.getmtime)

    @staticmethod
    def _unique_path(path: str) -> str:
        """同名ファイルが存在する場合、連番を付与する。"""
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        counter = 1
        while os.path.exists(f"{base}_{counter}{ext}"):
            counter += 1
        return f"{base}_{counter}{ext}"


# ---------------------------------------------------------------------------
# OBSController — OBS WebSocket v5 接続・リプレイバッファ保存
# ---------------------------------------------------------------------------
class OBSController:
    """OBS Studioとの通信を管理する。"""

    def __init__(self, host: str = "localhost", port: int = 4455,
                 password: str = "", replay_duration: int = 30):
        self.host = host
        self.port = port
        self.password = password
        self.replay_duration = replay_duration  # リプレイバッファ長（秒）
        self.client = None
        self.output_dir: str = ""

    def connect(self) -> bool:
        """OBS WebSocket v5 に接続し、出力ディレクトリを取得する。"""
        try:
            import obsws_python as obs
            self.client = obs.ReqClient(
                host=self.host, port=self.port, password=self.password
            )
            # 保存先ディレクトリを自動取得
            resp = self.client.get_record_directory()
            self.output_dir = resp.record_directory
            return True
        except Exception as e:
            raise ConnectionError(f"OBS接続失敗: {e}")

    def save_replay_buffer(self) -> bool:
        """リプレイバッファを保存する。"""
        if not self.client:
            raise ConnectionError("OBSに接続されていません")
        try:
            self.client.save_replay_buffer()
            return True
        except Exception as e:
            raise RuntimeError(f"リプレイバッファ保存失敗: {e}")

    def disconnect(self) -> None:
        """接続を切断する。"""
        self.client = None


# ---------------------------------------------------------------------------
# ChatMonitor — YouTube/Twitch チャット取得（別スレッド）
# ---------------------------------------------------------------------------
class ChatMonitor:
    """chat-downloader でライブチャットを取得し、コメントを処理する。"""

    def __init__(self, url: str, on_comment=None, on_excitement=None,
                 log_callback=None):
        """
        Args:
            url: YouTube/Twitch の配信URL
            on_comment: コメント受信時のコールバック (text, elapsed_sec)
            on_excitement: 盛り上がり検知時のコールバック (genre, top_comment, elapsed_sec)
            log_callback: ログ出力コールバック
        """
        self.url = url
        self.on_comment = on_comment
        self.on_excitement = on_excitement
        self.log_callback = log_callback
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.detector = ExcitementDetector()
        self.stream_start_time: float | None = None  # 配信開始時刻
        self.monitor_start_time: float = 0.0         # 監視開始時刻

    def start(self) -> None:
        """別スレッドでチャット監視を開始する。"""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """監視を停止する。"""
        self._stop_event.set()

    def _run(self) -> None:
        """チャット取得ループ（別スレッドで実行）。"""
        try:
            from chat_downloader import ChatDownloader

            self.monitor_start_time = time.time()
            if self.log_callback:
                self.log_callback("[ChatMonitor] チャット取得を開始します...")

            downloader = ChatDownloader()
            chat = downloader.get_chat(self.url, output=None)

            for message in chat:
                if self._stop_event.is_set():
                    break

                text = message.get("message", "")
                if not text:
                    continue

                # 配信開始からの経過時間を推定
                # chat-downloader の time_in_seconds を利用（あれば）
                elapsed = message.get("time_in_seconds")
                if elapsed is None:
                    elapsed = time.time() - self.monitor_start_time

                # コメントコールバック
                if self.on_comment:
                    self.on_comment(text, elapsed)

                # ExcitementDetector にコメント追加
                self.detector.add_comment(text)

                # 盛り上がり判定
                if self.detector.check_excitement():
                    genre = self.detector.classify_genre()
                    top_comment = self.detector.extract_top_comment()
                    if self.log_callback:
                        self.log_callback(
                            f"[検知] {genre} | トップ: {top_comment} | 経過: {int(elapsed)}秒"
                        )
                    if self.on_excitement:
                        self.on_excitement(genre, top_comment, elapsed)

        except Exception as e:
            if self.log_callback:
                self.log_callback(f"[ChatMonitor] エラー: {e}")


# ---------------------------------------------------------------------------
# SaaS プラン管理ヘルパー
# ---------------------------------------------------------------------------
def load_settings() -> dict:
    """設定ファイルを読み込む。存在しなければデフォルト値を返す。"""
    defaults = {
        "plan": "free",                   # "free" or "holiday_pass"
        "holiday_pass_start": None,       # ISO形式の開始日時
        "holiday_pass_end": None,         # ISO形式の終了日時
        "extended": False,                # 期間延長済みか
        "tutorial_done": False,           # チュートリアル完了か
        "registration_done": False,       # 登録完了か
        "daily_save_count": 0,            # 今日の保存回数
        "last_save_date": None,           # 最後に保存した日付（YYYY-MM-DD）
        "total_saves": 0,                 # 累計保存数
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # デフォルト値で補完
            for k, v in defaults.items():
                if k not in data:
                    data[k] = v
            return data
        except (json.JSONDecodeError, IOError):
            pass
    return defaults


def save_settings(settings: dict) -> None:
    """設定ファイルに書き込む。"""
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def is_holiday_pass_active(settings: dict) -> bool:
    """Holiday Pass が有効期間内か判定する。"""
    if settings.get("plan") != "holiday_pass":
        return False
    end_str = settings.get("holiday_pass_end")
    if not end_str:
        return False
    try:
        end_dt = datetime.fromisoformat(end_str)
        return datetime.now() < end_dt
    except (ValueError, TypeError):
        return False


def check_daily_limit(settings: dict) -> tuple[bool, int]:
    """Free版の日次保存上限をチェック。(上限内か, 残り回数) を返す。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if settings.get("last_save_date") != today:
        # 日付が変わっていたらリセット
        settings["daily_save_count"] = 0
        settings["last_save_date"] = today
        save_settings(settings)
    remaining = FREE_DAILY_LIMIT - settings["daily_save_count"]
    return remaining > 0, max(0, remaining)


# ---------------------------------------------------------------------------
# AutoClipApp — メインGUI（Tkinter）
# ---------------------------------------------------------------------------
class AutoClipApp:
    """Tkinterベースのメインアプリケーション。SaaSプラン管理を含む。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("AutoClip Pro")
        self.root.geometry("700x680")
        self.root.resizable(False, False)

        # 状態管理
        self.settings = load_settings()
        self.chat_monitor: ChatMonitor | None = None
        self.obs_controller: OBSController | None = None
        self.clip_namer: ClipNamer | None = None
        self.log_queue: queue.Queue = queue.Queue()
        self.is_monitoring = False

        # GUIの構築
        self._build_gui()

        # ログポーリング開始
        self._poll_log_queue()

        # 起動時チェック: Holiday Pass 期間延長のポップアップ
        self.root.after(500, self._check_extension_prompt)

    # ==================== GUI構築 ====================

    def _build_gui(self) -> None:
        """GUIウィジェットを構築する。"""
        # --- プランステータスバー ---
        status_frame = ttk.LabelFrame(self.root, text="プランステータス")
        status_frame.pack(fill="x", padx=10, pady=(10, 5))

        self.plan_label = ttk.Label(status_frame, text="", font=("", 11))
        self.plan_label.pack(padx=10, pady=5)
        self._update_plan_label()

        # --- URL入力 ---
        url_frame = ttk.LabelFrame(self.root, text="配信URL")
        url_frame.pack(fill="x", padx=10, pady=5)

        self.url_var = tk.StringVar()
        ttk.Entry(url_frame, textvariable=self.url_var, width=80).pack(
            padx=10, pady=5
        )

        # --- OBS設定 ---
        obs_frame = ttk.LabelFrame(self.root, text="OBS接続設定")
        obs_frame.pack(fill="x", padx=10, pady=5)

        row1 = ttk.Frame(obs_frame)
        row1.pack(fill="x", padx=10, pady=2)
        ttk.Label(row1, text="Host:").pack(side="left")
        self.obs_host_var = tk.StringVar(value="localhost")
        ttk.Entry(row1, textvariable=self.obs_host_var, width=15).pack(
            side="left", padx=5
        )
        ttk.Label(row1, text="Port:").pack(side="left")
        self.obs_port_var = tk.StringVar(value="4455")
        ttk.Entry(row1, textvariable=self.obs_port_var, width=8).pack(
            side="left", padx=5
        )
        ttk.Label(row1, text="Password:").pack(side="left")
        self.obs_pass_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.obs_pass_var, width=15, show="*").pack(
            side="left", padx=5
        )

        row2 = ttk.Frame(obs_frame)
        row2.pack(fill="x", padx=10, pady=2)
        ttk.Label(row2, text="リプレイバッファ長(秒):").pack(side="left")
        self.replay_dur_var = tk.StringVar(value="30")
        ttk.Entry(row2, textvariable=self.replay_dur_var, width=6).pack(
            side="left", padx=5
        )
        ttk.Label(row2, text="確認間隔(秒):").pack(side="left")
        self.check_interval_var = tk.StringVar(value="2")
        ttk.Entry(row2, textvariable=self.check_interval_var, width=6).pack(
            side="left", padx=5
        )
        ttk.Label(row2, text="タイムアウト(秒):").pack(side="left")
        self.timeout_var = tk.StringVar(value="60")
        ttk.Entry(row2, textvariable=self.timeout_var, width=6).pack(
            side="left", padx=5
        )

        # --- ボタン ---
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=10, pady=5)

        self.start_btn = ttk.Button(
            btn_frame, text="▶ 監視開始", command=self._start_monitoring
        )
        self.start_btn.pack(side="left", padx=5)

        self.stop_btn = ttk.Button(
            btn_frame, text="■ 監視停止", command=self._stop_monitoring,
            state="disabled"
        )
        self.stop_btn.pack(side="left", padx=5)

        ttk.Button(
            btn_frame, text="📁 保存先フォルダを開く",
            command=self._open_output_folder
        ).pack(side="left", padx=5)

        # --- ログ表示 ---
        log_frame = ttk.LabelFrame(self.root, text="ログ")
        log_frame.pack(fill="both", expand=True, padx=10, pady=(5, 10))

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=15, state="disabled", wrap="word",
            font=("Courier", 10)
        )
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)

    # ==================== プランステータス ====================

    def _update_plan_label(self) -> None:
        """プランステータスラベルを更新する。"""
        if is_holiday_pass_active(self.settings):
            end_dt = datetime.fromisoformat(self.settings["holiday_pass_end"])
            remaining = (end_dt - datetime.now()).days
            self.plan_label.config(
                text=f"🎫 Holiday Pass (Pro) — 残り{remaining}日 | YouTube + Twitch | 保存無制限",
                foreground="green"
            )
        else:
            # Free版 — 期限切れならfreeにリセット
            if self.settings.get("plan") == "holiday_pass":
                self.settings["plan"] = "free"
                save_settings(self.settings)
            ok, remaining = check_daily_limit(self.settings)
            self.plan_label.config(
                text=f"🆓 Free版 — 本日の残り保存回数: {remaining}/{FREE_DAILY_LIMIT} | YouTube のみ",
                foreground="blue"
            )

    # ==================== 監視制御 ====================

    def _start_monitoring(self) -> None:
        """監視を開始する。プランに応じたバリデーションを実施。"""
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("入力エラー", "配信URLを入力してください。")
            return

        # Free版でTwitch URLをブロック
        is_pro = is_holiday_pass_active(self.settings)
        if not is_pro and "twitch.tv" in url.lower():
            messagebox.showinfo(
                "Pro版限定機能",
                "Twitchの監視はHoliday Pass（Pro版）限定機能です。\n"
                "チュートリアルを完了してHoliday Passを獲得してください！"
            )
            return

        # Free版の日次上限チェック
        if not is_pro:
            ok, remaining = check_daily_limit(self.settings)
            if not ok:
                messagebox.showinfo(
                    "本日の無料枠終了",
                    "本日のFree版保存上限（5回）に達しました。\n"
                    "Holiday Passを取得すると無制限に保存できます！"
                )
                return

        # OBS接続
        try:
            replay_dur = int(self.replay_dur_var.get())
            self.obs_controller = OBSController(
                host=self.obs_host_var.get().strip(),
                port=int(self.obs_port_var.get()),
                password=self.obs_pass_var.get(),
                replay_duration=replay_dur,
            )
            self.obs_controller.connect()
            self._log(f"[OBS] 接続成功 — 保存先: {self.obs_controller.output_dir}")
        except Exception as e:
            messagebox.showerror("OBS接続エラー", str(e))
            return

        # ClipNamer初期化
        self.clip_namer = ClipNamer(
            check_interval=float(self.check_interval_var.get()),
            max_timeout=float(self.timeout_var.get()),
        )

        # ChatMonitor開始
        self.chat_monitor = ChatMonitor(
            url=url,
            on_comment=self._on_comment,
            on_excitement=self._on_excitement,
            log_callback=self._log,
        )
        self.chat_monitor.start()
        self.is_monitoring = True

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._log("[App] 監視を開始しました")

    def _stop_monitoring(self) -> None:
        """監視を停止する。"""
        if self.chat_monitor:
            self.chat_monitor.stop()
        if self.obs_controller:
            self.obs_controller.disconnect()
        self.is_monitoring = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._log("[App] 監視を停止しました")

    # ==================== コールバック ====================

    def _on_comment(self, text: str, elapsed: float) -> None:
        """コメント受信時（軽量ログのみ）。"""
        pass  # 全コメントをログに出すと重いのでスキップ

    def _on_excitement(self, genre: str, top_comment: str, elapsed: float) -> None:
        """盛り上がり検知時のメインハンドラ。"""
        # Free版の日次上限チェック
        is_pro = is_holiday_pass_active(self.settings)
        if not is_pro:
            ok, remaining = check_daily_limit(self.settings)
            if not ok:
                self._log("[App] 本日の無料枠終了 — 監視を停止します")
                self.root.after(0, lambda: messagebox.showinfo(
                    "本日の無料枠終了",
                    "本日のFree版保存上限（5回）に達しました。"
                ))
                self.root.after(100, self._stop_monitoring)
                return

        # OBSリプレイバッファ保存
        try:
            self.obs_controller.save_replay_buffer()
            self._log("[OBS] リプレイバッファ保存をトリガーしました")
        except Exception as e:
            self._log(f"[OBS] 保存失敗: {e}")
            return

        # 保存カウント更新
        self.settings["daily_save_count"] = self.settings.get("daily_save_count", 0) + 1
        self.settings["total_saves"] = self.settings.get("total_saves", 0) + 1
        save_settings(self.settings)
        self.root.after(0, self._update_plan_label)

        # ファイル名生成 & リネーム（別スレッドで実行）
        replay_dur = self.obs_controller.replay_duration
        filename = self.clip_namer.generate_filename(
            elapsed_seconds=elapsed,
            replay_duration=replay_dur,
            top_comment=top_comment,
            genre=genre,
        )
        self._log(f"[ClipNamer] 生成ファイル名: {filename}")

        output_dir = self.obs_controller.output_dir
        threading.Thread(
            target=self.clip_namer.wait_and_rename,
            args=(output_dir, filename, self._log),
            daemon=True,
        ).start()

        # チュートリアル完了チェック（初回保存）
        if not self.settings.get("tutorial_done"):
            self.settings["tutorial_done"] = True
            save_settings(self.settings)
            self.root.after(500, self._show_holiday_pass_offer)

    # ==================== Holiday Pass フロー ====================

    def _show_holiday_pass_offer(self) -> None:
        """チュートリアル完了後、Holiday Pass 獲得ポップアップを表示する。"""
        result = messagebox.askyesno(
            "🎉 Holiday Pass 獲得！",
            "おめでとうございます！初回の自動保存を達成しました。\n\n"
            "Holiday Pass（Pro版お試し・14日間）を無料で獲得できます！\n"
            "YouTube + Twitch 対応、保存回数無制限でお使いいただけます。\n\n"
            "登録画面に進みますか？"
        )
        if result:
            self._show_registration_window()

    def _show_registration_window(self) -> None:
        """ダミーのメールアドレス・カード登録ウィンドウを表示する。"""
        win = tk.Toplevel(self.root)
        win.title("Holiday Pass — アカウント登録")
        win.geometry("450x350")
        win.resizable(False, False)
        win.grab_set()  # モーダル化

        ttk.Label(win, text="🎫 Holiday Pass 登録",
                  font=("", 14, "bold")).pack(pady=10)

        form = ttk.Frame(win)
        form.pack(padx=30, pady=10, fill="x")

        ttk.Label(form, text="メールアドレス:").grid(
            row=0, column=0, sticky="w", pady=5
        )
        email_var = tk.StringVar(value="user@example.com")
        ttk.Entry(form, textvariable=email_var, width=30).grid(
            row=0, column=1, pady=5, padx=5
        )

        ttk.Label(form, text="カード番号:").grid(
            row=1, column=0, sticky="w", pady=5
        )
        card_var = tk.StringVar(value="4242-4242-4242-4242")
        ttk.Entry(form, textvariable=card_var, width=30).grid(
            row=1, column=1, pady=5, padx=5
        )

        ttk.Label(form, text="有効期限:").grid(
            row=2, column=0, sticky="w", pady=5
        )
        exp_var = tk.StringVar(value="12/28")
        ttk.Entry(form, textvariable=exp_var, width=10).grid(
            row=2, column=1, pady=5, padx=5, sticky="w"
        )

        ttk.Label(form, text="セキュリティコード:").grid(
            row=3, column=0, sticky="w", pady=5
        )
        cvc_var = tk.StringVar(value="123")
        ttk.Entry(form, textvariable=cvc_var, width=6).grid(
            row=3, column=1, pady=5, padx=5, sticky="w"
        )

        note = ttk.Label(
            win,
            text="※ これはプロトタイプのダミー画面です。\n   実際の課金は発生しません。",
            foreground="gray", font=("", 9)
        )
        note.pack(pady=5)

        def on_submit():
            win.destroy()
            self._show_survey_window()

        ttk.Button(win, text="登録して次へ →", command=on_submit).pack(pady=10)

    def _show_survey_window(self) -> None:
        """ダミーの初期アンケートウィンドウを表示する。"""
        win = tk.Toplevel(self.root)
        win.title("Holiday Pass — 初期アンケート")
        win.geometry("450x380")
        win.resizable(False, False)
        win.grab_set()

        ttk.Label(win, text="📋 初期アンケート",
                  font=("", 14, "bold")).pack(pady=10)

        form = ttk.Frame(win)
        form.pack(padx=30, pady=10, fill="x")

        ttk.Label(form, text="主にどのプラットフォームで配信しますか？").pack(
            anchor="w", pady=5
        )
        platform_var = tk.StringVar(value="YouTube")
        for p in ["YouTube", "Twitch", "両方"]:
            ttk.Radiobutton(form, text=p, variable=platform_var, value=p).pack(
                anchor="w", padx=20
            )

        ttk.Label(form, text="1回の配信時間は？").pack(anchor="w", pady=(10, 5))
        duration_var = tk.StringVar(value="1〜3時間")
        for d in ["1時間未満", "1〜3時間", "3時間以上"]:
            ttk.Radiobutton(form, text=d, variable=duration_var, value=d).pack(
                anchor="w", padx=20
            )

        ttk.Label(form, text="クリップの主な用途は？").pack(anchor="w", pady=(10, 5))
        usage_var = tk.StringVar(value="切り抜き動画")
        for u in ["切り抜き動画", "SNS共有", "アーカイブ", "その他"]:
            ttk.Radiobutton(form, text=u, variable=usage_var, value=u).pack(
                anchor="w", padx=20
            )

        def on_submit():
            # Holiday Pass を有効化
            now = datetime.now()
            self.settings["plan"] = "holiday_pass"
            self.settings["holiday_pass_start"] = now.isoformat()
            self.settings["holiday_pass_end"] = (
                now + timedelta(days=HOLIDAY_PASS_DAYS)
            ).isoformat()
            self.settings["registration_done"] = True
            save_settings(self.settings)
            self._update_plan_label()
            win.destroy()
            messagebox.showinfo(
                "🎉 登録完了！",
                f"Holiday Pass が有効になりました！\n"
                f"有効期限: {(now + timedelta(days=HOLIDAY_PASS_DAYS)).strftime('%Y年%m月%d日')}\n\n"
                f"YouTube + Twitch 対応、保存回数無制限でお楽しみください！"
            )
            self._log("[App] Holiday Pass を有効化しました")

        ttk.Button(win, text="回答を送信して登録完了 ✓", command=on_submit).pack(
            pady=15
        )

    def _check_extension_prompt(self) -> None:
        """起動時: Holiday Pass 残り7日未満なら延長アンケートを表示する。"""
        if not is_holiday_pass_active(self.settings):
            return
        if self.settings.get("extended", False):
            return  # 既に延長済み

        end_str = self.settings.get("holiday_pass_end", "")
        try:
            end_dt = datetime.fromisoformat(end_str)
        except (ValueError, TypeError):
            return

        remaining_days = (end_dt - datetime.now()).days
        if remaining_days >= EXTENSION_THRESHOLD_DAYS:
            return  # まだ余裕がある

        result = messagebox.askyesno(
            "🎫 Holiday Pass 期間延長",
            f"Holiday Passの残り期間が {remaining_days} 日です。\n\n"
            "短いアンケートに回答して、期間を1週間延長しませんか？\n"
            "（14日 → 21日）"
        )
        if result:
            self._show_extension_survey()

    def _show_extension_survey(self) -> None:
        """Pro版使用感アンケート → 期間延長。"""
        win = tk.Toplevel(self.root)
        win.title("使用感アンケート — 期間延長")
        win.geometry("450x400")
        win.resizable(False, False)
        win.grab_set()

        ttk.Label(win, text="📋 Pro版 使用感アンケート",
                  font=("", 14, "bold")).pack(pady=10)

        form = ttk.Frame(win)
        form.pack(padx=30, pady=10, fill="x")

        ttk.Label(form, text="AutoClip Proの満足度は？").pack(
            anchor="w", pady=5
        )
        sat_var = tk.StringVar(value="満足")
        for s in ["とても満足", "満足", "普通", "不満"]:
            ttk.Radiobutton(form, text=s, variable=sat_var, value=s).pack(
                anchor="w", padx=20
            )

        ttk.Label(form, text="最もよく使った機能は？").pack(
            anchor="w", pady=(10, 5)
        )
        feat_var = tk.StringVar(value="自動保存")
        for f in ["自動保存", "ファイル命名", "Twitch対応", "リアルタイムログ"]:
            ttk.Radiobutton(form, text=f, variable=feat_var, value=f).pack(
                anchor="w", padx=20
            )

        ttk.Label(form, text="改善してほしい点（自由記述）:").pack(
            anchor="w", pady=(10, 5)
        )
        feedback_text = tk.Text(form, height=3, width=40)
        feedback_text.pack(padx=5, pady=5)

        def on_submit():
            # 期間を+7日延長
            end_str = self.settings.get("holiday_pass_end", "")
            try:
                end_dt = datetime.fromisoformat(end_str)
            except (ValueError, TypeError):
                end_dt = datetime.now()
            new_end = end_dt + timedelta(days=EXTENSION_DAYS)
            self.settings["holiday_pass_end"] = new_end.isoformat()
            self.settings["extended"] = True
            save_settings(self.settings)
            self._update_plan_label()
            win.destroy()
            messagebox.showinfo(
                "✅ 期間延長完了！",
                f"Holiday Pass が延長されました！\n"
                f"新しい有効期限: {new_end.strftime('%Y年%m月%d日')}"
            )
            self._log(f"[App] Holiday Pass を {new_end.strftime('%Y-%m-%d')} まで延長しました")

        ttk.Button(win, text="回答を送信して期間延長 ✓", command=on_submit).pack(
            pady=15
        )

    # ==================== ユーティリティ ====================

    def _open_output_folder(self) -> None:
        """OBSの保存先フォルダをOSで開く。"""
        if self.obs_controller and self.obs_controller.output_dir:
            path = self.obs_controller.output_dir
        else:
            path = os.path.expanduser("~")
        import subprocess
        import sys
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])

    def _log(self, message: str) -> None:
        """スレッドセーフなログ追加。キューに入れてメインスレッドで処理。"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}")

    def _poll_log_queue(self) -> None:
        """キューからログメッセージを取り出して表示する（50msごと）。"""
        while not self.log_queue.empty():
            try:
                msg = self.log_queue.get_nowait()
                self.log_text.config(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
            except queue.Empty:
                break
        self.root.after(50, self._poll_log_queue)


# ---------------------------------------------------------------------------
# メインエントリーポイント
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    root = tk.Tk()
    app = AutoClipApp(root)
    root.mainloop()
