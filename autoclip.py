from __future__ import annotations
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoClip Pro — YouTube/Twitch Live 自動クリッパー (プロトタイプ)

盛り上がりを検知してOBSのリプレイバッファを自動保存・リネームする。
SaaSモデル: Free版（YouTube, 5回/日）/ Holiday Pass（YouTube+Twitch, 無制限, 14日間）

【システム全体の流れ】
1. ChatMonitor がライブチャットを別スレッドで取得し続ける
2. ExcitementDetector がコメントを分析して「盛り上がり」を判定する
3. 盛り上がりが検知されると OBSController がリプレイバッファを保存する
4. ClipNamer が保存されたファイルを意味のある名前にリネームする
5. AutoClipApp (GUI) が全体を統括し、SaaSプランの制限を管理する
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
    """
    【役割】ライブチャットのコメントを分析し「盛り上がり」を自動検知するエンジン。

    直近N秒間のコメント数が閾値を超えたとき「盛り上がり」と判定し、
    さらにそのコメント内容から「爆笑・称賛・失敗・困惑」などジャンルを分類、
    最も意味のあるコメントテキストを抽出する。
    クールダウン機能により、短時間での連続誤検知を防ぐ。
    """

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
        【役割】盛り上がり判定の各パラメータを設定し、内部バッファを初期化する。

        Args:
            window_sec: 分析対象とするコメントの時間幅（秒）。古いコメントは自動削除。
            threshold: 盛り上がり判定に必要な最低コメント数。これ未満は無視。
            cooldown_sec: 一度検知した後、次の検知を無視するクールダウン時間（秒）。
            genre_min_ratio: ジャンル確定に必要なキーワードの最低出現比率（これ未満は「注目シーン」）。
        """
        self.window_sec = window_sec
        self.threshold = threshold
        self.cooldown_sec = cooldown_sec
        self.genre_min_ratio = genre_min_ratio
        self.comments: deque = deque()          # (timestamp, text) のペア
        self.last_trigger_time: float = 0.0     # 最後にトリガーした時刻

    def add_comment(self, text: str, timestamp: float | None = None) -> None:
        """
        【役割】新しいコメントをバッファに追加し、window_sec より古いコメントを自動削除する。

        ChatMonitor から呼ばれ、常に「直近N秒間のコメントのみ」がバッファに残るよう管理する。
        これにより check_excitement() が常に最新のコメント量で判定できる。
        """
        ts = timestamp or time.time()
        self.comments.append((ts, text))
        cutoff = ts - self.window_sec
        while self.comments and self.comments[0][0] < cutoff:
            self.comments.popleft()

    def check_excitement(self) -> bool:
        """
        【役割】現在のバッファ内コメント数が閾値を超えており、かつクールダウン経過済みか判定する。

        True を返した場合、呼び出し元（ChatMonitor）は OBS のリプレイバッファ保存を発動する。
        クールダウンにより、一度盛り上がりが検知された後、短時間に連続トリガーされることを防ぐ。
        """
        if len(self.comments) < self.threshold:
            return False
        now = time.time()
        if now - self.last_trigger_time < self.cooldown_sec:
            return False
        self.last_trigger_time = now
        return True

    def classify_genre(self) -> str:
        """
        【役割】バッファ内のコメントを NG_GENRE_DICT のキーワードと照合し、シーンのジャンルを分類する。

        「爆笑シーン」「称賛シーン」「失敗シーン」「困惑シーン」「Twitch文化シーン」のいずれかを返す。
        最頻出ジャンルが全コメントの genre_min_ratio 未満のときは「注目シーン」を返す。
        戻り値はそのままクリップのファイル名の一部に使用される。
        """
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
        """
        【役割】バッファ内から「意味のある」コメントを抽出し、最も多く投稿されたテキストを返す。

        「www」「草」「GG」などの純粋なリアクションワードのみのコメントを除外し、
        視聴者が実際に言葉で表現した内容を見つける。
        戻り値（最大15文字）はクリップのファイル名（△△部分）として使用される。
        意味あるコメントが1件もない場合は「リアクション多数」を返す。
        """
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
        """
        【役割】コメントテキストから絵文字・顔文字を除去してサニタイズする。

        extract_top_comment() の前処理として使用。
        絵文字や顔文字を含むコメントを、テキスト部分だけに整理することで
        意味あるコメントの抽出精度を上げる。
        """
        t = self.RE_EMOJI.sub("", text)
        t = self.RE_KAOMOJI.sub("", t)
        t = t.strip()
        # 空白のみになった場合
        if not t:
            return ""
        return t

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """
        【役割】コメントテキストを単語（トークン）のリストに分割する。

        英語のように空白で区切られている場合は各単語に分割する。
        日本語のように空白がない場合はコメント全体を1トークンとして扱う。
        classify_genre() と extract_top_comment() がキーワード照合に使用する。
        """
        parts = text.split()
        if len(parts) <= 1:
            return [text.strip()] if text.strip() else []
        return [p for p in parts if p]


# ---------------------------------------------------------------------------
# ClipNamer — ファイル命名 + サイズ安定化待ち + リネーム
# ---------------------------------------------------------------------------
class ClipNamer:
    """
    【役割】OBS が保存したリプレイバッファファイルを、意味のある名前にリネームする。

    ファイル名の形式: HHMMSS_XmYs_トップコメント_ジャンル.mp4
    例: 012345_2m0s_神プレイ_称賛シーン.mp4

    OBS はリプレイバッファ保存後もファイルへの書き込みを続けることがあるため、
    ファイルサイズが安定（変化しなくなる）するまで待ってからリネームする。
    """

    # OSで禁止されるファイル名文字
    RE_FORBIDDEN = re.compile(r'[\\/:*?"<>|]')

    def __init__(self, check_interval: float = 2.0, max_timeout: float = 60.0):
        """
        【役割】ファイル書き込み完了チェックのタイミングパラメータを設定する。

        Args:
            check_interval: ファイルサイズを確認する間隔（秒）。短すぎると書き込み中にリネームしてしまう。
            max_timeout: この秒数待ってもファイルが安定しない場合はリネームをあきらめる。
        """
        self.check_interval = check_interval
        self.max_timeout = max_timeout

    def generate_filename(self, elapsed_seconds: float, replay_duration: int,
                          top_comment: str, genre: str) -> str:
        """
        【役割】クリップを後から探しやすくするための構造化されたファイル名を生成する。

        ファイル名に「配信開始からの経過時間・クリップ長・トップコメント・ジャンル」を
        埋め込むことで、ファイル一覧を見ただけでどの場面か把握できるようにする。

        生成形式: HHMMSS_XmYs_△△_〇〇シーン.mp4
          - HHMMSS: 配信開始からの経過時間（時分秒）
          - XmYs:   OBSリプレイバッファの録画長
          - △△:     視聴者のトップコメント（最大15文字）
          - 〇〇:   ジャンル（爆笑・称賛・失敗など）

        Args:
            elapsed_seconds: 配信開始からの経過秒数
            replay_duration: OBSリプレイバッファ長（秒）
            top_comment: ExcitementDetector が抽出したトップコメント
            genre: ExcitementDetector が分類したジャンル名
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
        【役割】OBS がファイルへの書き込みを完了するまで待機し、完了後にリネームする。

        OBS はリプレイバッファ保存の指令を受けた直後からファイルに書き込みを始めるが、
        書き込みが終わるまでリネームすると破損する可能性がある。
        そのため「前回確認時と現在のファイルサイズが同じ」になるまでポーリングで待つ。
        別スレッドから呼び出されるため、GUI をブロックしない。

        Args:
            output_dir: OBSの録画出力先ディレクトリ
            new_filename: generate_filename() で生成したリネーム後のファイル名
            log_callback: ログ出力用コールバック（GUIのログ欄に表示するため）

        Returns:
            リネーム後のフルパス（成功時）、またはNone（タイムアウト・エラー時）
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
        """
        【役割】ファイル名として使用できない記号（\\ / : * ? " < > |）を除去する。

        OSによって禁止されているファイル名文字を含む可能性のある
        コメントやジャンル名を、安全なファイル名文字列に変換する。
        """
        return self.RE_FORBIDDEN.sub("", text).strip()

    @staticmethod
    def _find_latest_mp4(directory: str) -> str | None:
        """
        【役割】指定ディレクトリ内で最も最近更新された mp4（または mkv）ファイルを返す。

        OBS がどんな名前でリプレイバッファを保存しても対応できるよう、
        ファイル名ではなく更新日時で「最新ファイル」を特定する。
        mp4 が見つからない場合は mkv もチェックする（OBSの出力設定依存）。
        """
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
        """
        【役割】指定パスに同名ファイルが存在する場合、連番を付加して重複を回避する。

        同じシーンで複数回クリップが保存された場合でも上書きを防ぐ。
        例: 012345_2m0s_神_称賛シーン.mp4 → 012345_2m0s_神_称賛シーン_1.mp4
        """
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
    """
    【役割】OBS Studio との WebSocket v5 通信を担当するコントローラー。

    obsws-python ライブラリを介して OBS に接続し、
    リプレイバッファの保存命令を送る。
    また接続時に OBS の録画出力先ディレクトリを自動取得し、
    ClipNamer がファイルを探す場所を提供する。
    """

    def __init__(self, host: str = "localhost", port: int = 4455,
                 password: str = "", replay_duration: int = 30):
        """
        【役割】OBS への接続に必要なパラメータを保持する。

        実際の接続は connect() を呼ぶまで行われない（遅延接続）。
        replay_duration は ClipNamer のファイル名生成（XmYs部分）に使用される。
        """
        self.host = host
        self.port = port
        self.password = password
        self.replay_duration = replay_duration  # リプレイバッファ長（秒）
        self.client = None
        self.output_dir: str = ""

    def connect(self) -> bool:
        """
        【役割】OBS WebSocket v5 に接続し、録画出力先ディレクトリを取得する。

        接続成功後に output_dir を自動で取得するため、
        ユーザーが手動で出力先を設定する手間を省く。
        接続失敗時は ConnectionError を発生させ、GUI 側でエラーダイアログを表示させる。
        """
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
        """
        【役割】OBS に「今すぐリプレイバッファを保存せよ」という命令を送る。

        盛り上がりが検知された瞬間に AutoClipApp._on_excitement() から呼ばれる。
        このメソッドが成功すると OBS がファイルを output_dir に書き出し始める。
        失敗時は RuntimeError を発生させ、ログに記録される。
        """
        if not self.client:
            raise ConnectionError("OBSに接続されていません")
        try:
            self.client.save_replay_buffer()
            return True
        except Exception as e:
            raise RuntimeError(f"リプレイバッファ保存失敗: {e}")

    def disconnect(self) -> None:
        """
        【役割】OBS との WebSocket 接続を切断し、クライアントを解放する。

        監視停止ボタンが押されたとき、または Free 版の上限に達したときに呼ばれる。
        """
        self.client = None


# ---------------------------------------------------------------------------
# ChatMonitor — YouTube/Twitch チャット取得（別スレッド）
# ---------------------------------------------------------------------------
class ChatMonitor:
    """
    【役割】YouTube / Twitch のライブチャットをリアルタイムで取得し、
    コメントを ExcitementDetector に流す監視スレッドを管理する。

    chat-downloader ライブラリを使ってコメントストリームを受信し、
    コメントを受け取るたびに ExcitementDetector へ渡す。
    盛り上がりが検知されると on_excitement コールバックを通じて
    AutoClipApp に通知し、OBS 保存をトリガーさせる。
    """

    def __init__(self, url: str, on_comment=None, on_excitement=None,
                 log_callback=None):
        """
        【役割】チャット監視に必要な URL・コールバック・内部コンポーネントを初期化する。

        Args:
            url: 監視対象の YouTube または Twitch 配信 URL
            on_comment: 各コメント受信時に呼ばれるコールバック (text, elapsed_sec)
            on_excitement: 盛り上がり検知時に呼ばれるコールバック (genre, top_comment, elapsed_sec)
            log_callback: ログをGUIに表示するためのコールバック
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
        """
        【役割】チャット監視を別スレッド（デーモンスレッド）として起動する。

        GUI スレッドをブロックせずにチャット取得を行うために別スレッドで動かす。
        デーモンスレッドのため、アプリ終了時に自動でスレッドも終了する。
        """
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """
        【役割】チャット監視ループに停止フラグを立て、スレッドを終了させる。

        _stop_event をセットすることで、_run() ループが次の反復で終了する。
        """
        self._stop_event.set()

    def _run(self) -> None:
        """
        【役割】別スレッドで実行されるチャット取得のメインループ。

        chat-downloader でコメントストリームを受信し続け、
        各コメントを ExcitementDetector に追加する。
        盛り上がりが検知されたら genre・top_comment・経過時間とともに
        on_excitement コールバックを呼び出す。
        停止フラグが立つか、エラーが発生するとループを抜ける。
        """
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
    """
    【役割】autoclip_settings.json からユーザーのプラン情報・利用履歴を読み込む。

    ファイルが存在しない（初回起動）または破損している場合は
    Free 版のデフォルト設定を返す。
    既存ファイルに新しいキーが不足している場合はデフォルト値で補完する。
    """
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
    """
    【役割】現在のプラン情報・利用カウントを autoclip_settings.json に書き込む。

    Holiday Pass の有効化・日次カウントの更新・期間延長など
    設定が変わるたびに呼ばれ、次回起動時にも状態が引き継がれるようにする。
    """
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def is_holiday_pass_active(settings: dict) -> bool:
    """
    【役割】現在時刻が Holiday Pass の有効期間内かどうかを判定する。

    plan が "holiday_pass" かつ holiday_pass_end が未来の日時であれば True を返す。
    この関数が False を返すと、アプリは Free 版の制限（YouTube のみ・5回/日）を適用する。
    """
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
    """
    【役割】Free 版ユーザーの「1日5回」クリップ保存上限を管理する。

    日付をまたいだ場合はカウントを自動リセットする。
    戻り値の1つ目は「まだ保存可能か（True/False）」、
    2つ目は「今日の残り保存可能回数」。
    _start_monitoring() と _on_excitement() の両方でチェックに使用される。
    """
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
    """
    【役割】システム全体を統括する Tkinter ベースのメインアプリケーション。

    GUI の構築・ユーザー入力の受付・各コンポーネント（OBSController / ChatMonitor /
    ClipNamer / ExcitementDetector）の生成と協調を担当する。
    また SaaS プランの制限（Free版/Holiday Pass）を監視・強制し、
    チュートリアル完了後の Holiday Pass 獲得フロー・期間延長フローを管理する。
    """

    def __init__(self, root: tk.Tk):
        """
        【役割】アプリ起動時の初期化処理をまとめる。

        設定ファイルを読み込み、GUI を構築し、ログポーリングを開始する。
        起動後 500ms 後に Holiday Pass の期限チェックを非同期で実施する。
        """
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
        """
        【役割】アプリ画面に表示される全 UI ウィジェットを生成・配置する。

        上から順に「プランステータスバー → URL入力 → OBS接続設定 →
        操作ボタン → ログ表示エリア」を構築する。
        各入力値は StringVar で保持し、監視開始時に読み取られる。
        """
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
        """
        【役割】現在のプラン状態を画面上部のステータスバーに反映する。

        Holiday Pass が有効なら残り日数と無制限表示（緑）、
        Free 版なら残り保存回数（青）を表示する。
        Holiday Pass が期限切れの場合は plan を "free" に自動ダウングレードする。
        保存が発生するたびに呼ばれ、リアルタイムで残り回数を更新する。
        """
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
        """
        【役割】「監視開始」ボタンが押されたときの処理を担う。

        入力バリデーション（URL未入力・Free版でのTwitch URL・日次上限）を行い、
        問題がなければ OBSController に接続し、ClipNamer と ChatMonitor を初期化して
        監視を開始する。バリデーションに失敗した場合はダイアログを表示して中断する。
        """
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
        """
        【役割】「監視停止」ボタンが押されたときにチャット監視と OBS 接続を終了する。

        ChatMonitor のスレッドに停止フラグを立て、OBSController の接続を切断する。
        ボタンの有効・無効状態を反転し、ユーザーが再度監視を開始できる状態に戻す。
        """
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
        """
        【役割】各コメント受信時に呼ばれるコールバック（現在は処理なし）。

        全コメントをログに出力するとパフォーマンスが低下するため意図的にスキップしている。
        将来的にコメント表示機能を追加する場合はここに実装する。
        """
        pass  # 全コメントをログに出すと重いのでスキップ

    def _on_excitement(self, genre: str, top_comment: str, elapsed: float) -> None:
        """
        【役割】盛り上がり検知時のメインハンドラ。システムの中核となる処理を担う。

        以下の順序で処理を実行する:
        1. Free 版の日次上限を再チェックし、超過なら監視を停止する
        2. OBSController.save_replay_buffer() でリプレイバッファを保存する
        3. 保存カウントをインクリメントし設定ファイルに書き込む
        4. ClipNamer.generate_filename() でファイル名を生成する
        5. 別スレッドで ClipNamer.wait_and_rename() を実行しリネームする
        6. 初回保存の場合、Holiday Pass 獲得ポップアップを表示する
        """
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
        """
        【役割】初回クリップ保存（チュートリアル完了）後に Holiday Pass 獲得を案内する。

        ユーザーが「はい」を選ぶと登録フロー（_show_registration_window）に進む。
        これが SaaS のコンバージョンファネルの入口となる。
        """
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
        """
        【役割】Holiday Pass 登録用のダミーフォーム（メール・クレジットカード）を表示する。

        現時点はプロトタイプのため実際の決済処理は行わない（ダミー画面）。
        「登録して次へ」を押すと _show_survey_window() へ進む。
        モーダルウィンドウとして表示し、登録完了まで他の操作をブロックする。
        """
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
        """
        【役割】Holiday Pass 有効化前の初期アンケートを収集し、回答後にパスを有効化する。

        配信プラットフォーム・配信時間・クリップ用途の3問を収集する。
        「回答を送信」後に settings の plan を "holiday_pass" に更新し、
        14日間の有効期限を設定して save_settings() で保存する。
        これがコンバージョンファネルの最終ステップとなる。
        """
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
        """
        【役割】起動時に Holiday Pass の残り期間を確認し、7日未満なら延長を提案する。

        既に延長済み（extended=True）の場合はスキップする。
        ユーザーが「はい」を選ぶと _show_extension_survey() を呼び出す。
        起動直後の 500ms 後に非同期で実行されるため、GUI 表示を妨げない。
        """
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
        """
        【役割】Pro版の使用感アンケートを収集し、回答後に Holiday Pass を7日延長する。

        満足度・よく使った機能・自由記述の3問を収集する。
        回答送信後に holiday_pass_end を +7日し、extended フラグを True にして保存する。
        これによりユーザーエンゲージメントの維持とフィードバック収集を同時に行う。
        """
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
        """
        【役割】OBS の録画出力先フォルダを OS のファイルエクスプローラーで開く。

        OBS に接続済みなら output_dir を、未接続ならホームディレクトリを開く。
        macOS / Windows / Linux に対応したコマンドを選択して実行する。
        """
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
        """
        【役割】別スレッドから安全にログメッセージを GUI のログ欄へ送るためのキューに積む。

        Tkinter はシングルスレッドのため、ChatMonitor などの別スレッドから
        直接ウィジェットを操作するとクラッシュする。
        そのためキューを介してメインスレッド（_poll_log_queue）に委ねる方式をとる。
        タイムスタンプを自動で付加する。
        """
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}")

    def _poll_log_queue(self) -> None:
        """
        【役割】メインスレッドで 50ms ごとにログキューを処理し、GUI のログ欄に表示する。

        root.after() で自己再帰的にスケジュールし、アプリ起動中は常に実行し続ける。
        キューにメッセージが溜まっている間は全て取り出して表示し、
        ScrolledText を末尾に自動スクロールすることで最新ログが常に見える状態を保つ。
        """
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
