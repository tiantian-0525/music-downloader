# -*- coding: utf-8 -*-
"""音乐下载器 —— 按歌手 / 频道 / 播放列表批量抓歌，转成 MP3 并洗好标签。

依赖：yt-dlp、ffmpeg（都已在 PATH 里）。不需要联网安装任何东西。
用法：双击同目录的「音乐下载器.bat」。
"""
import difflib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from tkinter import ttk, filedialog, messagebox

try:
    from mutagen.id3 import ID3, APIC, USLT, TIT2, TPE1, TPE2, TALB
    HAVE_MUTAGEN = True
except Exception:
    HAVE_MUTAGEN = False

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "配置.json")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
IS_WIN = os.name == "nt"


def ytdlp():
    return "yt-dlp.exe" if IS_WIN else "yt-dlp"


def ffmpeg():
    return "ffmpeg.exe" if IS_WIN else "ffmpeg"


def run(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW, timeout=timeout)


def mmss(s):
    try:
        s = int(float(s))
    except Exception:
        return "--:--"
    return "%d:%02d" % (s // 60, s % 60)


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|]', "_", s).strip()
    return s[:120] or "未命名"


def clean_title(t):
    """去掉 'Artist - ' 前缀和 (Official Audio) 这类尾巴，得到干净的曲名。"""
    s = re.sub(r"^.{0,40}?\s[-–—]\s", "", t)          # 开头的 "某某 - "
    s = re.sub(r"\s*[\(\（【\[][^\)\）】\]]*(Official|官方|Audio|Video|MV|Lyric|首播|完整版|高音质)[^\)\）】\]]*[\)\）】\]]",
               "", s, flags=re.I)
    s = re.sub(r"【[^】]*】", "", s)
    return s.strip(" -_")


UA = {"User-Agent": "MusicDownloader/1.0"}
BAD_VER = re.compile(r"live|現場|现场|karaoke|伴奏|instrumental|remix|cinemaphonic", re.I)


def http_json(url, timeout=20, headers=None):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# 繁简对照：iTunes 用繁体、网易云用简体，不比一下对不上。
T2S = dict(zip(
    "樂團樹潛裡這個們時間愛對夢鏡頭嗎麼誰還過說見讓開關聽寫讀錢長門陽陰雲風飛與為會來東"
    "車馬鳥魚龍龜歲麵著無處萬億點線題頁買賣貴邊遠進連運達選錯鐘鐵銀鋼針鎖閃閉問閒陣際"
    "隨難雖雙雜雞電燈聲書畫筆學習體發現實學問認識語言詞語講話討論題",
    "乐团树潜里这个们时间爱对梦镜头吗么谁还过说见让开关听写读钱长门阳阴云风飞与为会来东"
    "车马鸟鱼龙龟岁面着无处万亿点线题页买卖贵边远进连运达选错钟铁银钢针锁闪闭问闲阵际"
    "随难虽双杂鸡电灯声书画笔学习体发现实学问认识语言词语讲话讨论题"))


def norm(s):
    s = (s or "").lower()
    s = "".join(T2S.get(ch, ch) for ch in s)
    return re.sub(r"[^\w㐀-鿿]", "", s)


def title_keys(t):
    """歌名的几种写法：原样、去括号、中文段、破折号切开。用来对付中英混排的歌名。"""
    t = t or ""
    keys = {t}
    keys.add(re.sub(r"[\(（\[【].*?[\)）\]】]", " ", t))
    for m in re.findall(r"[㐀-鿿][㐀-鿿\s、・·＋+]*", t):
        keys.add(m)
    for part in re.split(r"[-–—－]", t):
        keys.add(part)
    return [norm(k) for k in keys if norm(k)]


def ratio(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def pick_match(cands, title, artist, dur):
    """从候选里挑最像的：歌名相似度为主，时长差为辅；现场版、伴奏这类排除。"""
    keys = title_keys(title)
    a_given = norm(artist)
    best, best_score = None, 0.0
    for c in cands:
        name = c.get("name") or ""
        if BAD_VER.search(name) and not BAD_VER.search(title):
            continue
        a_cand = norm(c.get("artist"))
        if a_given and a_cand and ratio(a_given, a_cand) < 0.5 \
                and a_given not in a_cand and a_cand not in a_given:
            continue
        ts = max(ratio(k, norm(name)) for k in keys) if keys else 0.0
        dd = abs((c.get("dur") or 0) - dur) if dur else 0
        if dd > 40:            # YouTube 配音常比唱片多出前奏尾奏，容差放宽
            continue
        score = ts - min(dd, 60) / 120.0
        if ts >= 0.72 and score > best_score:
            best, best_score = c, score
    return best


def artist_short(a):
    """艺人名里的中文段，或者第一个词。搜索时带上它比带全名准。"""
    m = re.search(r"[㐀-鿿][㐀-鿿\s、・·]*", a or "")
    if m:
        return m.group(0).strip()
    return (a or "").split()[0] if a else ""


def search_terms(title, artist):
    ts = [(title + " " + artist).strip()]
    head = artist_short(artist)
    if head:
        t2 = (title + " " + head).strip()
        if t2 not in ts:
            ts.append(t2)
    if title.strip() not in ts:
        ts.append(title.strip())
    return ts


def find_cover(title, artist, dur):
    """iTunes 搜专辑封面，返回 (封面字节, 专辑名)。几种查法合并候选。"""
    terms = search_terms(title, artist)
    cands, seen = [], set()
    for term in terms:
        try:
            j = http_json("https://itunes.apple.com/search?term=%s&entity=song&limit=25&country=tw"
                          % urllib.parse.quote(term))
        except Exception:
            continue
        for x in j.get("results", []):
            k = (x.get("trackName"), x.get("artistName"))
            if k in seen:
                continue
            seen.add(k)
            cands.append({"name": x.get("trackName", ""), "artist": x.get("artistName", ""),
                          "dur": (x.get("trackTimeMillis") or 0) / 1000.0,
                          "album": x.get("collectionName", ""), "url": x.get("artworkUrl100", "")})
    m = pick_match(cands, title, artist, dur)
    if not m or not m.get("url"):
        return None, None
    req = urllib.request.Request(m["url"].replace("100x100bb", "600x600bb"), headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(), m.get("album")


def find_lyrics(title, artist, dur):
    """网易云搜带时间轴的 LRC 歌词，返回文本或 None。"""
    H = dict(UA)
    H["Referer"] = "https://music.163.com/"
    cands, seen = [], set()
    for term in search_terms(title, artist):
        try:
            j = http_json("https://music.163.com/api/search/get?s=%s&type=1&limit=10"
                          % urllib.parse.quote(term), headers=H)
        except Exception:
            continue
        for s in ((j.get("result") or {}).get("songs") or []):
            if s.get("id") in seen:
                continue
            seen.add(s.get("id"))
            cands.append({"name": s.get("name", ""),
                          "artist": (s.get("artists") or [{}])[0].get("name", ""),
                          "dur": (s.get("duration") or 0) / 1000.0, "id": s.get("id")})
    m = pick_match(cands, title, artist, dur)
    if not m:
        return None
    lj = http_json("https://music.163.com/api/song/lyric?id=%s&lv=1&kv=1&tv=-1"
                   % m["id"], headers=H)
    lrc = ((lj.get("lrc") or {}).get("lyric") or "").strip()
    return lrc or None


def strip_ts(lrc):
    """去掉 LRC 的时间标签，得到纯文本。"""
    return "\n".join(re.sub(r"^\[\d+:\d+(?:\.\d+)?\]", "", ln).strip()
                     for ln in lrc.splitlines()).strip()


def write_tags(path, title, artist, album=None, cover=None, lyric=None):
    """写 ID3v2.3：标题 / 艺人 / 专辑 / 封面(APIC) / 歌词(USLT)。"""
    if not HAVE_MUTAGEN:
        return False
    try:
        try:
            tags = ID3(path)
        except Exception:
            tags = ID3()
        if title:
            tags.add(TIT2(encoding=3, text=title))
        if artist:
            tags.add(TPE1(encoding=3, text=artist))
            tags.add(TPE2(encoding=3, text=artist))
        if album:
            tags.add(TALB(encoding=3, text=album))
        if cover:
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        if lyric:
            tags.delall("USLT")
            tags.add(USLT(encoding=3, lang="chi", desc="", text=strip_ts(lyric)))
        tags.save(path, v2_version=3)
        return True
    except Exception:
        return False


def dur_of(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path])
    try:
        return float((r.stdout or "").strip())
    except Exception:
        return 0.0


def has_cover(path):
    if not HAVE_MUTAGEN:
        return False
    try:
        return bool(ID3(path).getall("APIC"))
    except Exception:
        return False


def load_conf():
    try:
        with open(CONF, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_conf(d):
    try:
        with open(CONF, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.conf = load_conf()
        self.items = []          # [{id,title,dur,checked,row}]
        self.raw = []            # 抓到的原始列表，筛选时从这里重算
        self.q = queue.Queue()
        self.worker = None
        self.stop_flag = False
        self.busy = False

        root.title("音乐下载器")
        root.geometry("980x760")
        root.minsize(880, 620)

        pad = {"padx": 8, "pady": 4}

        # ---- 来源 ----
        top = ttk.LabelFrame(root, text=" 1. 找歌 ")
        top.pack(fill="x", **pad)
        self.var_src = tk.StringVar()
        r1 = ttk.Frame(top)
        r1.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Button(r1, text="获取列表", command=self.fetch).pack(side="right", padx=(6, 0))
        e = ttk.Entry(r1, textvariable=self.var_src)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda _: self.fetch())
        ttk.Label(top, text="可填：歌手名 / 频道链接 / 播放列表链接 / 视频链接",
                  foreground="#777").pack(anchor="w", padx=10, pady=(0, 8))

        # ---- 列表 ----
        mid = ttk.LabelFrame(root, text=" 2. 挑歌 ")
        mid.pack(fill="both", expand=True, **pad)

        bar = ttk.Frame(mid)
        bar.pack(fill="x", padx=8, pady=(8, 0))
        self.var_onlyaudio = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="同曲只留音频版", variable=self.var_onlyaudio,
                        command=self.render).pack(side="left")
        self.var_nolong = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="略去整张专辑（>20 分钟）", variable=self.var_nolong,
                        command=self.render).pack(side="left", padx=(12, 0))
        ttk.Button(bar, text="全选", width=6, command=lambda: self.check_all(True)).pack(side="left", padx=(16, 4))
        ttk.Button(bar, text="全不选", width=7, command=lambda: self.check_all(False)).pack(side="left", padx=4)
        self.lbl_count = ttk.Label(bar, text="0 首")
        self.lbl_count.pack(side="right")

        wrap = ttk.Frame(mid)
        wrap.pack(fill="both", expand=True, padx=8, pady=8)
        cols = ("dur", "status")
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings", height=10)
        self.tree.heading("#0", text="曲目")
        self.tree.heading("dur", text="时长")
        self.tree.heading("status", text="状态")
        self.tree.column("#0", width=620)
        self.tree.column("dur", width=70, anchor="center", stretch=False)
        self.tree.column("status", width=110, anchor="center", stretch=False)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.tag_configure("done", foreground="#2e7d32")
        self.tree.tag_configure("fail", foreground="#c62828")

        # ---- 输出 ----
        bot = ttk.LabelFrame(root, text=" 3. 存到哪 ")
        bot.pack(fill="x", **pad)
        self.var_out = tk.StringVar(value=self.conf.get("out", os.path.expanduser("~/Desktop/音乐")))
        r3 = ttk.Frame(bot)
        r3.pack(fill="x", padx=8, pady=8)
        self.var_artist = tk.StringVar(value=self.conf.get("artist", ""))
        ttk.Entry(r3, textvariable=self.var_artist, width=18).pack(side="right")
        ttk.Label(r3, text="艺人标签").pack(side="right", padx=(12, 4))
        ttk.Button(r3, text="浏览", width=6, command=self.pick_out).pack(side="right", padx=(8, 8))
        ttk.Entry(r3, textvariable=self.var_out).pack(side="left", fill="x", expand=True)

        # ---- 操作 ----
        act = ttk.Frame(root)
        act.pack(fill="x", padx=8)
        self.btn_go = ttk.Button(act, text="开始下载", command=self.start)
        self.btn_go.pack(side="left", pady=4)
        self.btn_stop = ttk.Button(act, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(act, text="重试失败的", command=self.retry_failed).pack(side="left", padx=8)
        ttk.Button(act, text="打开文件夹", command=self.open_out).pack(side="left", padx=8)
        ttk.Button(act, text="给文件夹补封面", command=self.fill_folder).pack(side="left", padx=8)
        self.fill_meta = tk.BooleanVar(value=self.conf.get("fill", True))
        ttk.Checkbutton(act, text="下完配封面和歌词",
                        variable=self.fill_meta).pack(side="right", padx=(4, 6))
        self.save_lrc = tk.BooleanVar(value=self.conf.get("lrc", False))
        ttk.Checkbutton(act, text="另存 .lrc", variable=self.save_lrc).pack(side="right", padx=(4, 8))

        self.pb = ttk.Progressbar(root, mode="determinate")
        self.pb.pack(fill="x", padx=16, pady=(6, 2))
        self.var_stat = tk.StringVar(value="就绪")
        ttk.Label(root, textvariable=self.var_stat, foreground="#555").pack(anchor="w", padx=16)

        logf = ttk.Frame(root)
        logf.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log = tk.Text(logf, height=6, wrap="word", state="disabled",
                           font=("Consolas", 9))
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self.pump)

    # ---------- 基础 ----------
    def say(self, msg):
        self.q.put(("log", msg))

    def pump(self):
        try:
            while True:
                kind, *args = self.q.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", args[0] + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "status":
                    self.var_stat.set(args[0])
                elif kind == "rows":
                    self.refresh_rows()
                elif kind == "item":
                    idx, status, tag = args
                    rid = self.items[idx]["row"]
                    self.tree.set(rid, "status", status)
                    self.tree.item(rid, tags=(tag,) if tag else ())
                elif kind == "progress":
                    done, total = args
                    self.pb.configure(maximum=max(1, total), value=done)
                elif kind == "list":
                    self.fill_list(args[0])
                elif kind == "finish":
                    self.busy = False
                    self.btn_go.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self.pump)

    def on_close(self):
        if self.busy and not messagebox.askokcancel("退出", "还有下载在进行，确定退出？"):
            return
        self.stop_flag = True
        save_conf({"out": self.var_out.get(), "artist": self.var_artist.get(),
                   "fill": self.fill_meta.get(), "lrc": self.save_lrc.get()})
        self.root.destroy()

    # ---------- 列表 ----------
    def fill_list(self, items):
        self.raw = items
        if not self.var_artist.get() and items:
            ch = (items[0].get("channel") or "").strip()
            if ch:
                self.var_artist.set(ch)
        self.render()

    def render(self):
        """按两个筛选开关，从原始列表重算要显示的曲目。"""
        self.items = []
        for it in self.tree.get_children():
            self.tree.delete(it)
        items = list(self.raw)
        if self.var_onlyaudio.get():
            # 同一首歌若同时有 Official Audio 和 MV/Lyric，只留 Audio
            keep = {}
            for it in items:
                key = clean_title(it["title"])
                has_audio = "official audio" in it["title"].lower()
                if key not in keep or (has_audio and not keep[key][1]):
                    keep[key] = (it, has_audio)
            items = [v[0] for v in keep.values()]
        if self.var_nolong.get():
            items = [i for i in items if 0 <= (i.get("dur") or 0) <= 1200]
        for it in items:
            rid = self.tree.insert("", "end", text="☐ " + it["title"],
                                   values=(mmss(it["dur"]), ""))
            self.items.append(dict(it, checked=False, row=rid))
        self.var_stat.set("共 %d 首，勾选后开始下载" % len(self.items))
        self.update_count()

    def refresh_rows(self):
        for it in self.items:
            self.tree.item(it["row"], text=("☑ " if it["checked"] else "☐ ") + it["title"])
        self.update_count()

    def update_count(self):
        n = sum(1 for i in self.items if i["checked"])
        self.lbl_count.configure(text="已选 %d / 共 %d 首" % (n, len(self.items)))

    def on_click(self, ev):
        if self.tree.identify_region(ev.x, ev.y) not in ("tree", "cell"):
            return
        rid = self.tree.identify_row(ev.y)
        if not rid:
            return
        for it in self.items:
            if it["row"] == rid:
                it["checked"] = not it["checked"]
                self.tree.item(rid, text=("☑ " if it["checked"] else "☐ ") + it["title"])
                self.update_count()
                return

    def check_all(self, v):
        for it in self.items:
            if not it.get("failed"):
                it["checked"] = v
        self.refresh_rows()

    # ---------- 抓列表 ----------
    def fetch(self):
        if self.busy:
            return
        src = self.var_src.get().strip()
        if not src:
            messagebox.showinfo("提示", "先填歌手名或链接")
            return
        if not re.match(r"^(https?://|ytsearch:|bilisearch:)", src):
            src = "ytsearch40:" + src
        self.var_stat.set("正在获取列表…")
        threading.Thread(target=self._fetch, args=(src,), daemon=True).start()

    def _fetch(self, src):
        try:
            r = run([ytdlp(), "--flat-playlist", "--dump-json", "--no-warnings",
                     "--ignore-errors", src], timeout=600)
            items = []
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                vid = j.get("id")
                if not vid:
                    continue
                items.append({
                    "id": vid,
                    "title": j.get("title") or vid,
                    "dur": j.get("duration") or 0,
                    "channel": j.get("channel") or j.get("uploader") or "",
                    "url": j.get("url") or ("https://www.youtube.com/watch?v=" + vid),
                })
            if not items:
                self.q.put(("log", "没抓到内容：" + (r.stderr or "")[-300:]))
                self.q.put(("status", "列表为空"))
                return
            self.q.put(("list", items))
        except Exception as e:
            self.q.put(("log", "获取失败：%s" % e))
            self.q.put(("status", "获取失败"))

    # ---------- 下载 ----------
    def start(self):
        if self.busy:
            return
        todo = [i for i, it in enumerate(self.items) if it["checked"]]
        if not todo:
            messagebox.showinfo("提示", "还没勾选曲目")
            return
        out = self.var_out.get().strip()
        if not out:
            messagebox.showinfo("提示", "先选保存目录")
            return
        os.makedirs(out, exist_ok=True)
        save_conf({"out": out, "artist": self.var_artist.get(),
                   "fill": self.fill_meta.get(), "lrc": self.save_lrc.get()})
        self.busy = True
        self.stop_flag = False
        self.btn_go.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.worker = threading.Thread(target=self._download, args=(todo, out, self.var_artist.get().strip()),
                                       daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_flag = True
        self.var_stat.set("正在停止…（当前这首下完就停）")

    def retry_failed(self):
        for it in self.items:
            if it.get("failed"):
                it["failed"] = False
                it["checked"] = True
        self.refresh_rows()
        self.start()

    def _download(self, todo, out, artist):
        tmp = os.path.join(out, "_临时")
        os.makedirs(tmp, exist_ok=True)
        done = 0
        total = len(todo)
        self.q.put(("progress", 0, total))
        blocked_hits = 0
        for k, idx in enumerate(todo):
            if self.stop_flag:
                break
            it = self.items[idx]
            self.q.put(("item", idx, "下载中…", ""))
            self.q.put(("status", "(%d/%d) %s" % (k + 1, total, it["title"])))
            ok, err = self._one(it, tmp, out, artist)
            if not ok and self._blocked(err):
                # 撞上 YouTube 风控：等一会儿自动接着来
                blocked_hits += 1
                if blocked_hits > 5:
                    self.q.put(("item", idx, "风控停", "fail"))
                    self.items[idx]["failed"] = True
                    self.q.put(("log", "连续 6 次触发登录验证，先停下。稍后再点「重试失败的」。"))
                    break
                wait = 15
                self.q.put(("log", "YouTube 要求登录验证（第 %d 次），等 %d 分钟后自动重试…" % (blocked_hits, wait)))
                for m in range(wait, 0, -1):
                    if self.stop_flag:
                        break
                    self.q.put(("status", "被限流，%d 分钟后自动重试…" % m))
                    time.sleep(60)
                if self.stop_flag:
                    break
                self.q.put(("item", idx, "重试中…", ""))
                ok, err = self._one(it, tmp, out, artist)
            if ok:
                done += 1
                self.q.put(("item", idx, "✓ 完成", "done"))
            else:
                self.q.put(("item", idx, "✗ 失败", "fail"))
                self.items[idx]["failed"] = True
                self.q.put(("log", "失败：%s —— %s" % (it["title"], (err or "")[:160].replace("\n", " "))))
            self.q.put(("progress", done, total))
        try:
            os.rmdir(tmp)
        except Exception:
            pass
        self.q.put(("status", "结束：成功 %d / %d" % (done, total)))
        self.q.put(("finish",))

    @staticmethod
    def _blocked(err):
        e = (err or "").lower()
        return "not a bot" in e or "sign in" in e or "登录" in e

    def _one(self, it, tmp, out, artist):
        src = os.path.join(tmp, it["id"] + ".mp3")
        try:
            if not os.path.exists(src):
                r = run([ytdlp(), "-x", "--audio-format", "mp3", "--audio-quality", "0",
                         "--no-playlist", "--no-warnings", "--sleep-requests", "2",
                         "-o", os.path.join(tmp, "%(id)s.%(ext)s"), it["url"]], timeout=1800)
                if not os.path.exists(src):
                    return False, (r.stderr or r.stdout or "")[-300:]
            name = safe_name(clean_title(it["title"]))
            dst = os.path.join(out, name + ".mp3")
            n = 2
            while os.path.exists(dst):
                dst = os.path.join(out, "%s (%d).mp3" % (name, n))
                n += 1
            tag = os.path.join(tmp, "_tag.mp3")
            if os.path.exists(tag):
                os.remove(tag)
            meta = [ffmpeg(), "-v", "error", "-y", "-i", src, "-c", "copy",
                    "-metadata", "title=" + clean_title(it["title"]),
                    "-id3v2_version", "3", tag]
            if artist:
                meta[-1:-1] = ["-metadata", "artist=" + artist,
                               "-metadata", "album_artist=" + artist]
            r = run(meta)
            if r.returncode != 0 or not os.path.exists(tag):
                # 打标签失败就直接改名搬走，别让一首歌拖垮整批
                os.replace(src, dst)
            else:
                os.replace(tag, dst)
            if getattr(self, "fill_meta", None) is not None and self.fill_meta.get():
                self.dress(dst, clean_title(it["title"]), artist)
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "超时"
        except Exception as e:
            return False, str(e)

    def dress(self, path, title, artist):
        """给刚落地的文件配封面和歌词：iTunes 取图、网易云取 LRC。"""
        d = dur_of(path)
        cover = album = lrc = None
        try:
            cover, album = find_cover(title, artist, d)
        except Exception as e:
            self.q.put(("log", "  封面没查到：%s" % str(e)[:70]))
        try:
            lrc = find_lyrics(title, artist, d)
        except Exception as e:
            self.q.put(("log", "  歌词没查到：%s" % str(e)[:70]))
        write_tags(path, title, artist, album, cover, lrc)
        if lrc and self.save_lrc.get():
            try:
                with open(os.path.splitext(path)[0] + ".lrc", "w", encoding="utf-8") as f:
                    f.write(lrc)
            except Exception:
                pass
        got = [x for x in ("封面" if cover else "", "歌词" if lrc else "") if x]
        self.q.put(("log", "  %s —— %s" % (os.path.basename(path),
                                           "、".join(got) if got else "没配到封面和歌词")))

    # ---------- 给已有文件补封面 ----------
    def fill_folder(self):
        if self.busy:
            return
        out = self.var_out.get().strip()
        if not os.path.isdir(out):
            messagebox.showinfo("提示", "保存目录不存在")
            return
        if not HAVE_MUTAGEN:
            messagebox.showerror("缺组件", "写封面要 mutagen：\npython -m pip install mutagen")
            return
        if not self.var_artist.get().strip():
            messagebox.showinfo("提示", "先在「艺人标签」里填上歌手名，配封面要靠它认人")
            return
        self.busy = True
        self.stop_flag = False
        self.btn_go.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        threading.Thread(target=self._fill_folder, args=(out, self.var_artist.get().strip()),
                         daemon=True).start()

    def _fill_folder(self, out, artist):
        files = sorted(f for f in os.listdir(out) if f.lower().endswith(".mp3"))
        todo = [f for f in files if not has_cover(os.path.join(out, f))]
        self.q.put(("progress", 0, max(1, len(todo))))
        self.q.put(("log", "文件夹里 %d 首，%d 首还没有封面" % (len(files), len(todo))))
        got = 0
        for i, f in enumerate(todo, 1):
            if self.stop_flag:
                break
            p = os.path.join(out, f)
            title = re.sub(r"^.*?\s-\s", "", os.path.splitext(f)[0])
            self.q.put(("status", "(%d/%d) %s" % (i, len(todo), f)))
            cover = None
            try:
                cover, _ = find_cover(title, artist, dur_of(p))
            except Exception as e:
                self.q.put(("log", "  %s 查询失败：%s" % (f, str(e)[:60])))
            if cover and write_tags(p, None, None, None, cover, None):
                got += 1
                self.q.put(("log", "  ✓ %s" % f))
            else:
                self.q.put(("log", "  × %s 没配上" % f))
            self.q.put(("progress", i, len(todo)))
        self.q.put(("status", "补封面结束：成功 %d / %d" % (got, len(todo))))
        self.q.put(("finish",))

    # ---------- 杂项 ----------
    def pick_out(self):
        d = filedialog.askdirectory(initialdir=self.var_out.get() or os.path.expanduser("~"))
        if d:
            self.var_out.set(d)

    def open_out(self):
        d = self.var_out.get().strip()
        if d and os.path.isdir(d):
            os.startfile(d) if IS_WIN else subprocess.Popen(["xdg-open", d])


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        style.theme_use("vista" if IS_WIN else "clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
