# -*- coding: utf-8 -*-
"""
fosu - FNF (PsychEngine/Kade/旧版官方) -> osu!mania 一键转谱器
GUI 主程序（tkinter，标准库，无第三方依赖）。
用法:
  双击 fosu.exe                 打开图形界面
  fosu.exe --selftest 谱面.json Inst.ogg Voices.ogg 输出.osz   命令行自测
  fosu.exe --keys <目标K> <谱面.json> [输出.json] [--from <原K>] [--no-damage]
  fosu.exe --swap <谱面.json> [输出.json]        玩家<->对手音符互换
  fosu.exe --merge <谱面.json> [输出.json] [--target player|opponent]
                   [--conflict other_side|nearest|drop] [--reverse]
                                                    合并玩家轨道（一人唱两，合唱不互扰）
  fosu.exe --osu2fnf <谱面.osu> [输出.json] [--offset ms] [--audio 文件]
  fosu.exe --osu2mod <包.osz/.olz/.osu> [输出目录] [--offset ms] [--speed 2.4] [--no-week] [--chart-only]
"""
import os
import sys
import glob as _glob
import shutil
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fosu_core as fc

APP_NAME = "fosu"
APP_VER = "1.4 · v5.2 (20261121)"

DIFF_WORDS = {
    "easy": "Easy", "normal": "Normal", "hard": "Hard", "harder": "Harder",
    "insane": "Insane", "expert": "Expert", "expert+": "ExpertPlus",
    "erect": "Erect", "nightmare": "Nightmare", "hell": "Hell",
}


def guess_version(chart_path: str) -> str:
    stem = os.path.splitext(os.path.basename(chart_path))[0].lower()
    for token in stem.replace("_", "-").split("-"):
        token = token.strip()
        if token in DIFF_WORDS:
            return DIFF_WORDS[token]
    return "Normal"


def guess_artist(chart_path: str) -> str:
    # songs/<mod名>/<song名>/chart.json -> mod 名；桌面/下载等散文件 -> FNF
    d = os.path.dirname(chart_path)
    parent = os.path.basename(d)
    loose = ("desktop", "downloads", "documents", "songs", "song", "",
             "桌面", "下载", "文档")
    # v5.1：Psych 的谱在 <mod>/data/<歌>.json，旧逻辑把目录名 "data" 当作者
    if parent.lower() in ("data", "charts", "chart", "levels"):
        gp = os.path.basename(os.path.dirname(d))
        if gp.lower() in ("mods", "mod", "songs", "song", "assets", ""):
            return "FNF"
        return gp or "FNF"
    if parent.lower() in loose:
        return "FNF"
    grand = os.path.basename(os.path.dirname(d))
    if grand.lower() in ("mods", "mod", "songs", "song"):
        return parent
    return parent or "FNF"


def find_audio(chart_dir: str, names):
    if not os.path.isdir(chart_dir):
        return ""
    lower_map = {f.lower(): f for f in os.listdir(chart_dir)}
    for n in names:
        if n.lower() in lower_map:
            return os.path.join(chart_dir, lower_map[n.lower()])
    return ""


def ffmpeg_ready() -> bool:
    """ffmpeg 可用性判定（v5.1 修 bug）：
    fc.find_ffmpeg() 兜底返回裸名 "ffmpeg"（只在 PATH 里），
    旧代码 os.path.isfile("ffmpeg") 恒为 False → 明明能用也弹「缺少 ffmpeg」拦死导出。
    打包版是 _MEIPASS 里的实体文件，两路都认。"""
    p = fc.find_ffmpeg()
    return os.path.isfile(p) or (shutil.which(p) is not None)


# ------------------------------------------------------------ 拖拽导入 (Win WM_DROPFILES)
if os.name == "nt":
    import ctypes
    from ctypes import wintypes, WINFUNCTYPE

    _user32 = ctypes.windll.user32
    # 注意：DragAcceptFiles / DragQueryFileW / DragFinish 在 SHELL32，不在 user32
    _shell32 = ctypes.windll.shell32
    _WM_DROPFILES = 0x0233
    _GWL_WNDPROC = -4
    # 64 位下必须显式声明 restype，否则旧窗口过程指针会被截断成 32 位导致崩溃
    _user32.SetWindowLongPtrW.restype = wintypes.LPARAM
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    _user32.CallWindowProcW.restype = wintypes.LPARAM
    _user32.CallWindowProcW.argtypes = [wintypes.LPARAM, wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
    # HDROP 是 64 位句柄，不声明 argtypes 会被 ctypes 按 int(32 位) 截断而崩
    _shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    _shell32.DragQueryFileW.restype = wintypes.UINT
    _shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                        wintypes.LPWSTR, wintypes.UINT]
    _shell32.DragFinish.argtypes = [wintypes.HANDLE]

    class _DropCatcher:
        """给 Tk/Toplevel 挂上 WM_DROPFILES；回调收到拖入的文件绝对路径列表。

        关键：窗口过程里【绝不调用 Tk】——在 ctypes 回调内再进 Tcl 会触发
        GIL 致命错误（PyEval_RestoreThread ... thread state is NULL）。
        所以这里只把文件追加进队列，由 _schedule/_poll 在正常 Python 上下文里分发。
        """
        _alive = []  # 持引用，防止 WNDPROC 闭包被 GC

        def __init__(self, win, callback):
            self.win = win
            self.cb = callback
            self._pending = []
            self._stopped = False
            hwnd = win.winfo_id()
            self._proc = WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)(self._wndproc)
            self._old = _user32.SetWindowLongPtrW(hwnd, _GWL_WNDPROC,
                                                  ctypes.cast(self._proc, ctypes.c_void_p))
            _shell32.DragAcceptFiles(hwnd, True)
            _DropCatcher._alive.append(self)
            win.bind("<Destroy>", lambda e: self.stop(), add="+")
            self._schedule()

        def stop(self):
            self._stopped = True

        def _schedule(self):
            if self._stopped:
                return
            try:
                self.win.after(50, self._poll)
            except Exception:
                self._stopped = True

        def _poll(self):
            if self._stopped:
                return
            pending, self._pending = self._pending, []
            if pending:
                try:
                    self.cb(pending)
                except Exception:
                    pass
            self._schedule()

        def _wndproc(self, hwnd, msg, wp, lp):
            if msg == _WM_DROPFILES:
                self._on_drop(wp)
                return 0
            return _user32.CallWindowProcW(self._old, hwnd, msg, wp, lp)

        def _on_drop(self, hdrop):
            files = []
            try:
                n = _shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                for i in range(n):
                    size = _shell32.DragQueryFileW(hdrop, i, None, 0)
                    if size <= 0:
                        continue
                    buf = ctypes.create_unicode_buffer(size + 1)
                    _shell32.DragQueryFileW(hdrop, i, buf, size + 1)
                    files.append(buf.value)
            finally:
                _shell32.DragFinish(hdrop)
            # 只入队，不碰 Tk
            if files:
                self._pending.extend(files)

    def enable_drop(win, callback):
        try:
            win.update_idletasks()
            return _DropCatcher(win, callback)
        except Exception:
            return None
else:
    def enable_drop(win, callback):
        return None


AUDIO_EXTS = (".ogg", ".mp3", ".wav", ".flac", ".m4a")
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".wmv")


def run_headless(chart_path, inst, voices, out_osz, log=print):
    """命令行自测 / 也可被外部调用。"""
    song = fc.load_chart(chart_path)
    layout = fc.detect_layout(song)
    version = guess_version(chart_path)
    opts = fc.ConvertOptions(
        side="bf", filter_damage=True, keys_override=0, version=version,
        artist=guess_artist(chart_path), creator=APP_NAME,
    )
    return fc.convert_all(
        chart_path, out_osz, opts,
        inst_path=inst or None, voices_path=voices or None,
        mix=bool(inst), vol_inst=1.0, vol_voices=1.0,
        ffmpeg=fc.find_ffmpeg(), log=log, keep_workdir=False)


class FosuGUI:
    # 单一版本：转换器 + 内置中文编谱器，全部功能常驻（不再区分精简/完整）。
    LITE = False

    def __init__(self, root: tk.Tk):
        self.root = root
        self.chart_var = tk.StringVar()
        self.inst_var = tk.StringVar()
        self.voices_var = tk.StringVar()
        self.video_var = tk.StringVar()
        self.mix_var = tk.BooleanVar(value=True)
        self.inst_vol = tk.DoubleVar(value=1.0)
        self.voices_vol = tk.DoubleVar(value=1.0)
        self.keys_var = tk.StringVar(value="自动")
        self.side_var = tk.StringVar(value="BF（玩家）")
        self.damage_var = tk.BooleanVar(value=True)
        self.version_var = tk.StringVar(value="Normal")
        self.artist_var = tk.StringVar(value="FNF")
        self.hp_var = tk.DoubleVar(value=4.0)
        self.od_var = tk.DoubleVar(value=4.0)
        self.offset_var = tk.DoubleVar(value=0.0)
        self.sm_var = tk.DoubleVar(value=1.4)
        self.out_var = tk.StringVar()
        self.info_var = tk.StringVar(value="尚未选择谱面")
        self._build()
        self._check_ffmpeg()

    # ------------------------------------------------------------ UI
    def _build(self):
        self.root.title(f"{APP_NAME} {APP_VER} — FNF 转谱 / 编谱 一体化工具")
        # 尺寸自适应屏幕；内容放可滚动容器里，小屏不会看不到底部按钮
        sh = self.root.winfo_screenheight()
        sw = self.root.winfo_screenwidth()
        h = max(620, min(900, sh - 100))
        w = max(820, min(960, sw - 80))
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(820, 560)
        pad = {"padx": 8, "pady": 3}

        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True, side="top")
        cv = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        cv.configure(yscrollcommand=sb.set)
        frm = ttk.Frame(cv, padding=10)
        win_id = cv.create_window((0, 0), window=frm, anchor="nw")
        cv.bind("<Configure>", lambda e: cv.itemconfig(win_id, width=e.width))
        frm.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        # 主窗口内任意控件滚轮 → 滚动主界面（编辑器是独立 Toplevel，不受影响）
        self.root.bind("<MouseWheel>",
                       lambda e: cv.yview_scroll(int(-e.delta / 120), "units"))
        frm.columnconfigure(1, weight=1)

        # ⓪ 快捷入口
        quick = ttk.Frame(frm)
        quick.grid(row=0, column=0, columnspan=3, sticky="ew", padx=4, pady=(2, 6))
        ttk.Label(quick, text="快捷功能：", font=("Microsoft YaHei UI", 10, "bold"),
                  foreground="#246").pack(side="left")
        ttk.Button(quick, text="🎼 打开内置编谱器",
                   command=self.open_editor_window).pack(side="left", padx=(4, 4))
        ttk.Button(quick, text="📝 用当前谱面编谱",
                   command=self.open_current_in_editor).pack(side="left", padx=2)
        ttk.Button(quick, text="🔄 玩家↔对手互换",
                   command=self.open_swap_window).pack(side="left", padx=(8, 4))
        ttk.Button(quick, text="🧬 合并玩家轨道（一人唱两）",
                   command=self.open_merge_window).pack(side="left", padx=2)
        ttk.Button(quick, text="🎹 OSU→FNF（含音乐转 OGG）",
                   command=self.open_osu2fnf_window).pack(side="left", padx=2)
        ttk.Label(quick, text="（旧版谱面 / 多难度 / 键数互转 在下方）",
                  foreground="#888").pack(side="left", padx=8)

        # ① 谱面
        self._section(frm, 1, "① 选择 PsychEngine 谱面 JSON")
        ttk.Label(frm, text="谱面：").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.chart_var).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="浏览…", command=self.pick_chart).grid(row=2, column=2, **pad)
        ttk.Label(frm, textvariable=self.info_var, foreground="#0a6",
                  justify="left", wraplength=780).grid(row=3, column=0, columnspan=3,
                                                       sticky="w", padx=8)

        # ② 音频 / 视频
        self._section(frm, 4, "② 音频 / 视频（选中谱面后自动识别同目录；也可把文件拖进窗口）")
        ttk.Label(frm, text="Inst：").grid(row=5, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.inst_var).grid(row=5, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="浏览…", command=lambda: self.pick_audio(self.inst_var)).grid(
            row=5, column=2, **pad)
        ttk.Label(frm, text="Voices：").grid(row=6, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.voices_var).grid(row=6, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="浏览…", command=lambda: self.pick_audio(self.voices_var)).grid(
            row=6, column=2, **pad)

        row7 = ttk.Frame(frm)
        row7.grid(row=7, column=0, columnspan=3, sticky="w", padx=8)
        ttk.Label(row7, text="视频：").pack(side="left")
        ttk.Entry(row7, textvariable=self.video_var, width=36).pack(side="left")
        ttk.Button(row7, text="浏览…", command=self.pick_video).pack(side="left", padx=2)
        ttk.Button(row7, text="清空", command=lambda: self.video_var.set("")).pack(side="left")
        ttk.Label(row7, text="  （可选；无音频时自动用它的音轨）",
                  foreground="#888").pack(side="left")

        row8 = ttk.Frame(frm)
        row8.grid(row=8, column=0, columnspan=3, sticky="w", padx=8)
        ttk.Checkbutton(row8, text="混音 Inst + Voices", variable=self.mix_var).pack(side="left")
        ttk.Label(row8, text="  Inst 音量").pack(side="left")
        ttk.Spinbox(row8, from_=0, to=2, increment=0.05, width=5,
                    textvariable=self.inst_vol).pack(side="left")
        ttk.Label(row8, text="  Voices 音量").pack(side="left")
        ttk.Spinbox(row8, from_=0, to=2, increment=0.05, width=5,
                    textvariable=self.voices_vol).pack(side="left")

        # ③ 谱面选项
        self._section(frm, 9, "③ 谱面选项")
        row10 = ttk.Frame(frm)
        row10.grid(row=10, column=0, columnspan=3, sticky="w", padx=8)
        ttk.Label(row10, text="每侧键数：").pack(side="left")
        ttk.Combobox(row10, textvariable=self.keys_var, width=8, state="readonly",
                     values=["自动", "4", "5", "6", "7", "8", "9"]).pack(side="left")
        ttk.Label(row10, text="  导出音符：").pack(side="left")
        ttk.Combobox(row10, textvariable=self.side_var, width=12, state="readonly",
                     values=["BF（玩家）", "对手", "全部"]).pack(side="left")
        ttk.Checkbutton(row10, text="过滤扣血音符（noteType 3）",
                        variable=self.damage_var).pack(side="left", padx=12)

        row11 = ttk.Frame(frm)
        row11.grid(row=11, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        ttk.Label(row11, text="难度名：").pack(side="left")
        ttk.Entry(row11, textvariable=self.version_var, width=12).pack(side="left")
        ttk.Label(row11, text="  艺术家：").pack(side="left")
        ttk.Entry(row11, textvariable=self.artist_var, width=18).pack(side="left")

        row12 = ttk.Frame(frm)
        row12.grid(row=12, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        ttk.Label(row12, text="HP：").pack(side="left")
        ttk.Spinbox(row12, from_=1, to=10, increment=0.5, width=5,
                    textvariable=self.hp_var).pack(side="left")
        ttk.Label(row12, text="  OD：").pack(side="left")
        ttk.Spinbox(row12, from_=1, to=10, increment=0.5, width=5,
                    textvariable=self.od_var).pack(side="left")
        ttk.Label(row12, text="  时间偏移(ms)：").pack(side="left")
        ttk.Spinbox(row12, from_=-2000, to=2000, increment=10, width=7,
                    textvariable=self.offset_var).pack(side="left")
        ttk.Label(row12, text="  mania 滚动倍率：").pack(side="left")
        ttk.Spinbox(row12, from_=0.5, to=4, increment=0.1, width=5,
                    textvariable=self.sm_var).pack(side="left")

        # ④ 输出
        self._section(frm, 13, "④ 输出 .osz（osu! 谱面包，双击即可导入 osu!）")
        ttk.Label(frm, text="输出：").grid(row=14, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.out_var).grid(row=14, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="另存为…", command=self.pick_out).grid(row=14, column=2, **pad)

        self.go_btn = ttk.Button(frm, text="开始转换（→ osu!mania）", command=self.on_go)
        self.go_btn.grid(row=15, column=0, columnspan=2, pady=8)
        ttk.Button(frm, text="🔧 键数互转(4K↔多K)",
                   command=self.open_keys_window).grid(
            row=15, column=2, sticky="e", pady=8)
        ttk.Button(frm, text="📦 多难度批量打包（多难度 → 一个 osz）",
                   command=self.open_multi_window).grid(row=16, column=0, columnspan=2,
                                                        pady=2)
        ttk.Label(frm, text="可逐个添加难度并自定义难度名", foreground="#888"
                  ).grid(row=16, column=2, sticky="w")
        ttk.Button(frm, text="🎁 OSU 包 → FNF 模组（.osz/.olz/.osu → mods/）",
                   command=self.open_osumod_window).grid(
            row=17, column=0, columnspan=2, pady=2)
        ttk.Label(frm, text="整包转换：谱面+音频+封面+week 一次生成",
                  foreground="#888").grid(row=17, column=2, sticky="w")

        self.logbox = tk.Text(frm, height=12, wrap="word", state="disabled",
                              background="#111", foreground="#ddd", font=("Consolas", 9))
        self.logbox.grid(row=18, column=0, columnspan=3, sticky="nsew", padx=8, pady=4)
        frm.rowconfigure(18, weight=1)

        self.status = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status, relief="sunken",
                  anchor="w").pack(fill="x", side="bottom")

    def _section(self, frm, row, text):
        ttk.Label(frm, text=text, font=("Microsoft YaHei UI", 10, "bold"),
                  foreground="#246").grid(row=row, column=0, columnspan=3,
                                          sticky="w", padx=4, pady=(10, 2))

    # ------------------------------------------------------------ actions
    def _check_ffmpeg(self):
        ff = fc.find_ffmpeg()
        if os.path.isfile(ff):
            self.status.set(f"就绪　|　ffmpeg：内置 ({os.path.basename(ff)})")
        else:
            self.status.set("就绪　|　ffmpeg：未找到，将无法处理音频")

    def log(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", str(msg) + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")
        self.root.update_idletasks()

    def pick_chart(self):
        p = filedialog.askopenfilename(
            title="选择 FNF 谱面 JSON",
            filetypes=[("FNF 谱面", "*.json"), ("所有文件", "*.*")])
        if not p:
            return
        self.chart_var.set(p)
        self.after_pick(p)

    def after_pick(self, p):
        try:
            song, cinfo = fc.load_chart_info(p)
            layout = fc.detect_layout(song)
            keys = layout["keys"]
            st = fc.chart_stats(song, keys)
            bf = fc.collect_notes(song, side="bf", keys=keys)
            holds = sum(1 for n in bf if n["length"] > 0)
            bpm = song.get("bpm", "?")
            title = fc.prettify(str(song.get("song", os.path.basename(p))))
            mf = layout["mania_field"]
            mf_txt = f"有（={mf}，与实际不符时已自动按实际轨道）" if mf is not None else "无（普通 4K 谱）"
            engine = fc.ENGINE_CN.get(cinfo.get("engine", ""), cinfo.get("engine", ""))
            self.info_var.set(
                f"曲名：{title}　BPM：{bpm}　引擎：{engine}　mania 字段：{mf_txt}\n"
                f"实际：{keys}K/侧　BF 音符：{st['bf']}（长按 {holds}）　"
                f"对手音符：{st['opp']}　扣血音符：{st['damage']}")
            self.keys_var.set("自动")
            self.version_var.set(guess_version(p))
            self.artist_var.set(guess_artist(p))
            d = os.path.dirname(p)
            if not self.inst_var.get():
                inst = find_audio(d, ["Inst.ogg", "Inst.mp3", "Inst.wav",
                                      "inst.ogg", "instrumental.ogg",
                                      "Instrumental.ogg"])
                if inst:
                    self.inst_var.set(inst)
            if not self.voices_var.get():
                v = find_audio(d, ["Voices.ogg", "Voices.mp3", "Voices.wav",
                                   "voices.ogg", "Vocals.ogg", "vocals.ogg"])
                if v:
                    self.voices_var.set(v)
            # v5.1：换谱后输出名必须跟着换，否则会沿用上一首的 .osz 路径
            name = fc.safe_name(
                f"{self.artist_var.get()} - {title} (fosu)[{self.version_var.get()}].osz")
            self.out_var.set(os.path.join(d, name))
            self.log(f"已载入谱面：{p}")
            self.log(f"引擎探测：{fc.ENGINE_CN.get(cinfo.get('engine', ''), cinfo.get('engine', ''))}")
            for w in cinfo.get("fixed", []):
                self.log("🔧 旧版格式归一化：" + w)
            for w in cinfo.get("warnings", []):
                self.log("⚠ " + w)
            for w in layout["warnings"]:
                self.log("⚠ " + w)
            for w in fc.detect_timing(song)["warnings"]:
                self.log("⚠ " + w)
        except Exception as e:
            self.info_var.set("谱面读取失败：" + str(e))
            messagebox.showerror("读取失败", str(e))

    def pick_audio(self, var):
        p = filedialog.askopenfilename(
            title="选择音频", filetypes=[("音频", "*.ogg *.mp3 *.wav"), ("所有文件", "*.*")])
        if p:
            var.set(p)

    def pick_video(self):
        p = filedialog.askopenfilename(
            title="选择视频（可选）",
            filetypes=[("视频", "*.mp4 *.mov *.mkv *.avi *.flv *.webm *.wmv"),
                       ("所有文件", "*.*")])
        if p:
            self.video_var.set(p)

    def on_drop_files(self, files):
        """拖拽导入（万能入口）：
        .json→谱面（重复拖入会替换，v5.1 修旧版「已载入过就静默忽略」）
        .osu→打开 OSU→FNF 窗口并自动载入
        .osz/.olz/.zip/含 .osu 的目录→打开 OSU 包→FNF 模组窗口并自动扫描
        音频→按文件名归到 Inst/Voices；视频→视频。"""
        got = []
        for p in files:
            if os.path.isdir(p):
                try:
                    has_osu = any(f.lower().endswith('.osu')
                                  for f in os.listdir(p))
                except OSError:
                    has_osu = False
                if has_osu:
                    self.open_osumod_window(p)
                    got.append("OSU 包目录 " + os.path.basename(p.rstrip("\\/")))
                continue
            if not os.path.isfile(p):
                continue
            low = p.lower()
            base = os.path.basename(p)
            if low.endswith(".json"):
                self.chart_var.set(p)          # 总是替换：换歌/换难度是常态
                self.after_pick(p)
                got.append("谱面 " + base)
            elif low.endswith(".osu"):
                self.open_osu2fnf_window(p)
                got.append("osu 谱面 " + base)
            elif low.endswith((".osz", ".olz", ".zip")):
                self.open_osumod_window(p)
                got.append("OSU 包 " + base)
            elif low.endswith(AUDIO_EXTS):
                bl = base.lower()
                if "voice" in bl or "vocal" in bl:
                    self.voices_var.set(p)
                elif "inst" in bl or "instrument" in bl:
                    self.inst_var.set(p)
                elif not self.inst_var.get():
                    self.inst_var.set(p)
                else:
                    self.voices_var.set(p)
                got.append("音频 " + base)
            elif low.endswith(VIDEO_EXTS):
                self.video_var.set(p)
                got.append("视频 " + base)
        if got:
            self.log("已导入：" + "；".join(got))
            self.status.set("已导入 " + str(len(got)) + " 个文件")
        else:
            self.status.set("没有可识别的文件（支持 .json / .osu / .osz / .olz / .zip / 音频 / 视频 / 目录）")

    def pick_out(self):
        p = filedialog.asksaveasfilename(
            title="输出 osu! 谱面包", defaultextension=".osz",
            filetypes=[("osu! 谱面包", "*.osz")])
        if p:
            self.out_var.set(p)

    def on_go(self):
        chart = self.chart_var.get().strip()
        if not chart or not os.path.isfile(chart):
            messagebox.showwarning("缺少谱面", "请先选择有效的谱面 JSON")
            return
        out = self.out_var.get().strip()
        if not out:
            messagebox.showwarning("缺少输出路径", "请指定输出的 .osz 路径")
            return
        if not out.lower().endswith(".osz"):
            out += ".osz"
            self.out_var.set(out)
        inst = self.inst_var.get().strip()
        voices = self.voices_var.get().strip() if self.mix_var.get() else ""
        video = self.video_var.get().strip()
        if inst and not os.path.isfile(inst):
            messagebox.showwarning("音频缺失", f"找不到 Inst 音频：\n{inst}")
            return
        if voices and not os.path.isfile(voices):
            messagebox.showwarning("音频缺失", f"找不到 Voices 音频：\n{voices}")
            return
        if video and not os.path.isfile(video):
            messagebox.showwarning("视频缺失", f"找不到视频文件：\n{video}")
            return
        if not (inst or voices or video):
            # v5.1：不再硬拦。没音频也能导出（谱面照常可用，osu 里静音），
            # 旧版在这里直接 return，是「无法导出」的常见原因。
            if not messagebox.askyesno(
                    "没有音频",
                    "没检测到 Inst / Voices / 视频音轨。\n\n"
                    "继续导出会得到一个无声的 osz（谱面正常，音频需之后补 audio.ogg）。\n"
                    "现在继续导出吗？"):
                return
        # 音频/视频都要 ffmpeg 转码，先给个明确提示，别等到半路才报 WinError 2
        if not ffmpeg_ready():
            messagebox.showerror(
                "缺少 ffmpeg",
                "处理音频/视频需要 ffmpeg.exe，但当前找不到。\n\n"
                "解决办法（任选其一）：\n"
                "1）把 ffmpeg.exe 放到本 exe 同一目录；\n"
                "2）把 ffmpeg.exe 所在目录加入系统 PATH。")
            return

        keys_sel = self.keys_var.get()
        keys_override = 0 if keys_sel == "自动" else int(keys_sel)
        side_map = {"BF（玩家）": "bf", "对手": "opp", "全部": "all"}
        opts = fc.ConvertOptions(
            side=side_map.get(self.side_var.get(), "bf"),
            filter_damage=self.damage_var.get(),
            keys_override=keys_override,
            version=self.version_var.get().strip() or "Normal",
            hp=float(self.hp_var.get()), od=float(self.od_var.get()),
            slider_multiplier=float(self.sm_var.get()),
            offset_ms=float(self.offset_var.get()),
            artist=self.artist_var.get().strip() or "FNF",
            creator=APP_NAME,
            video_path=video,
            video_name=(fc.safe_name(os.path.basename(video)) if video else ""),
        )
        self.go_btn.configure(state="disabled")
        self.status.set("转换中…")
        t = threading.Thread(target=self._worker,
                             args=(chart, out, opts, inst, voices), daemon=True)
        t.start()

    def _worker(self, chart, out, opts, inst, voices):
        try:
            song = fc.load_chart(chart)
            layout = fc.detect_layout(song, opts.keys_override)
            for w in layout["warnings"]:
                self.root.after(0, self.log, "⚠ " + w)
            for w in fc.detect_timing(song)["warnings"]:
                self.root.after(0, self.log, "⚠ " + w)
            info = fc.convert_all(
                chart, out, opts,
                inst_path=inst or None, voices_path=voices or None,
                mix=bool(inst), vol_inst=float(self.inst_vol.get()),
                vol_voices=float(self.voices_vol.get()),
                ffmpeg=fc.find_ffmpeg(), log=lambda m: self.root.after(0, self.log, m))
            self.root.after(0, self._done, True, out, info)
        except Exception:
            tb = traceback.format_exc()
            self.root.after(0, self.log, tb)
            self.root.after(0, self._done, False, out, tb)

    def _done(self, ok, out, info):
        self.go_btn.configure(state="normal")
        if ok:
            self.status.set("转换完成")
            self.log(f"✔ 完成：{out}")
            if messagebox.askyesno("完成", f"谱面包已生成：\n{out}\n\n是否打开所在文件夹？"):
                os.startfile(os.path.dirname(out))
        else:
            self.status.set("转换失败")
            messagebox.showerror("转换失败", str(info))

    # ------------------------------------------------------------ 谱面编辑器窗口
    def open_editor_window(self, chart_path=""):
        """打开内置 FNF 谱面编辑器（PE 格式兼容 · 全汉化 · 可导入音频播放）。"""
        import fosu_editor
        # 没指定谱面时，自动带上主界面当前选的那份
        if not chart_path:
            cur = self.chart_var.get().strip()
            if cur and os.path.isfile(cur):
                chart_path = cur
        ed = fosu_editor.open_editor(self.root, chart_path, self.log)
        enable_drop(ed.win, lambda files: self._editor_drop(ed, files))
        return ed

    def open_current_in_editor(self):
        """用主界面当前谱面直接打开编谱器。"""
        p = self.chart_var.get().strip()
        if not p or not os.path.isfile(p):
            messagebox.showwarning("没选谱面", "请先在①里选一份 FNF 谱面 JSON", parent=self.root)
            return
        self.open_editor_window(p)

    def _editor_drop(self, ed, files):
        """往编谱器拖文件：JSON 当谱面，音频当 BGM（自动播放）。"""
        for p in files:
            low = p.lower()
            if low.endswith(".json") and os.path.isfile(p):
                ed.open_file(p)
                self.log("编辑器已载入谱面：" + p)
                return
            if any(low.endswith(e) for e in
                   (".ogg", ".mp3", ".wav", ".flac", ".m4a", ".mp4", ".aac", ".wma", ".opus")) \
                    and os.path.isfile(p):
                ed.load_audio_files([p])
                self.log("编辑器已导入音频：" + p)
                return

    # ------------------------------------------------------------ 合并玩家轨道窗口
    def open_merge_window(self):
        w = tk.Toplevel(self.root)
        w.title("合并玩家轨道 · 一人唱两（合唱不互扰）")
        w.geometry("780x700")
        try:
            w.tk.call("wm", "iconphoto", w._w, self.root._w)
        except Exception:
            pass
        pad = {"padx": 8, "pady": 3}
        chart_v = tk.StringVar(value=self.chart_var.get().strip())
        out_v = tk.StringVar()
        info_v = tk.StringVar(value="尚未选择谱面")
        target_v = tk.StringVar(value="player")
        conflict_v = tk.StringVar(value="other_side")
        tol_v = tk.StringVar(value="10")
        rev_v = tk.BooleanVar(value=False)

        ttk.Label(w, text="用途：把「玩家一段、对手一段」的正常对唱谱，合成"
                          "一个人打两段（一人唱两）。\n"
                          "合唱段双方同一时刻同一方向都有音符时会打架（互扰），"
                          "本工具自动把冲突音符让到另一边。\n"
                          "依据 FNF 官方语义：mustHitSection 决定哪半边是玩家，"
                          "等价于官方编辑器的 Swap Section。",
                  foreground="#888", wraplength=740, justify="left",
                  ).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 6))

        ttk.Label(w, text="① 谱面 JSON：").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(w, textvariable=chart_v).grid(row=2, column=0, columnspan=2,
                                                sticky="ew", **pad)
        ttk.Button(w, text="浏览…",
                   command=lambda: self._merge_pick(chart_v, out_v, info_v, w)
                   ).grid(row=2, column=2, **pad)
        ttk.Label(w, textvariable=info_v, foreground="#0a6", justify="left",
                  wraplength=740).grid(row=3, column=0, columnspan=3, sticky="w", padx=8)

        ttk.Label(w, text="② 合并到：").grid(row=4, column=0, sticky="w", **pad)
        tf = ttk.Frame(w)
        tf.grid(row=4, column=1, columnspan=2, sticky="w", **pad)
        ttk.Radiobutton(tf, text="玩家侧（一人唱两，推荐）",
                        variable=target_v, value="player").pack(side="left")
        ttk.Radiobutton(tf, text="对手侧（反向）",
                        variable=target_v, value="opponent").pack(side="left", padx=8)

        ttk.Label(w, text="③ 合唱冲突：").grid(row=5, column=0, sticky="w", **pad)
        cf = ttk.Frame(w)
        cf.grid(row=5, column=1, columnspan=2, sticky="w", **pad)
        ttk.Radiobutton(cf, text="让到另一边（不互扰，推荐）",
                        variable=conflict_v, value="other_side").pack(side="left")
        ttk.Radiobutton(cf, text="挪到最近空轨",
                        variable=conflict_v, value="nearest").pack(side="left", padx=6)
        ttk.Radiobutton(cf, text="丢弃",
                        variable=conflict_v, value="drop").pack(side="left", padx=6)

        of = ttk.Frame(w)
        of.grid(row=6, column=0, columnspan=3, sticky="w", padx=8, pady=4)
        ttk.Label(of, text="同刻容差(ms)：").pack(side="left")
        ttk.Spinbox(of, from_=0, to=100, increment=1, width=6,
                    textvariable=tol_v).pack(side="left")
        ttk.Checkbutton(of, text="先整体反转两侧再合并（把正常谱反过来干成一人唱两）",
                        variable=rev_v).pack(side="left", padx=12)

        ttk.Label(w, text="③ 输出谱面 JSON：").grid(row=7, column=0, sticky="w", **pad)
        ttk.Entry(w, textvariable=out_v).grid(row=8, column=0, columnspan=2,
                                              sticky="ew", **pad)
        ttk.Button(w, text="另存为…",
                   command=lambda: self._pick_swap_out(w, out_v)).grid(row=8, column=2, **pad)

        ttk.Button(w, text="🧬 开始合并（一人唱两）",
                   command=lambda: self._merge_worker(
                       w, chart_v, out_v, target_v, conflict_v, tol_v, rev_v)
                   ).grid(row=9, column=0, columnspan=3, pady=10)

        lb = tk.Text(w, height=13, wrap="word", state="disabled",
                     background="#111", foreground="#ddd", font=("Consolas", 9))
        lb.grid(row=10, column=0, columnspan=3, sticky="nsew", padx=8, pady=4)
        w.rowconfigure(10, weight=1)
        w.columnconfigure(0, weight=1)

        def slog(m):
            lb.configure(state="normal")
            lb.insert("end", str(m) + "\n")
            lb.see("end")
            lb.configure(state="disabled")
            w.update_idletasks()
        w._slog = slog
        enable_drop(w, lambda files: self._merge_drop(chart_v, out_v, info_v, w, files))
        if chart_v.get() and os.path.isfile(chart_v.get()):
            self._merge_show_info(chart_v.get(), out_v, info_v, w)

    def _merge_pick(self, chart_v, out_v, info_v, w):
        p = filedialog.askopenfilename(
            title="选择 FNF 谱面 JSON", parent=w,
            filetypes=[("FNF 谱面", "*.json"), ("所有文件", "*.*")])
        if p:
            chart_v.set(p)
            self._merge_show_info(p, out_v, info_v, w)

    def _merge_show_info(self, p, out_v, info_v, w):
        try:
            song, cinfo = fc.load_chart_info(p)
            keys = fc.detect_layout(song)["keys"]
            st = fc.chart_stats(song, keys)
            engine = fc.ENGINE_CN.get(cinfo.get("engine", ""), "")
            info_v.set(f"引擎：{engine}　{keys}K/侧　"
                       f"玩家音符 {st['bf']}　对手音符 {st['opp']}")
            base = os.path.splitext(os.path.basename(p))[0]
            out_v.set(os.path.join(os.path.dirname(p), f"{base}-merged.json"))
            for fx in cinfo.get("fixed", []):
                w._slog("🔧 旧版格式归一化：" + fx)
            for wn in cinfo.get("warnings", []):
                w._slog("⚠ " + wn)
            w._slog("已载入谱面：" + p)
        except Exception as e:
            info_v.set("读取失败：" + str(e))
            messagebox.showerror("读取失败", str(e), parent=w)

    def _merge_drop(self, chart_v, out_v, info_v, w, files):
        for p in files:
            if p.lower().endswith(".json") and os.path.isfile(p):
                chart_v.set(p)
                self._merge_show_info(p, out_v, info_v, w)
                return

    def _merge_worker(self, w, chart_v, out_v, target_v, conflict_v, tol_v, rev_v):
        chart = chart_v.get().strip()
        out = out_v.get().strip()
        if not chart or not os.path.isfile(chart):
            messagebox.showwarning("缺少谱面", "请先选择谱面 JSON", parent=w)
            return
        if not out:
            base = os.path.splitext(os.path.basename(chart))[0]
            out = os.path.join(os.path.dirname(chart), f"{base}-merged.json")
            out_v.set(out)
        elif not out.lower().endswith(".json"):
            out += ".json"
            out_v.set(out)
        try:
            tol = float(tol_v.get())
        except ValueError:
            tol = 10.0

        def run():
            try:
                res = fc.merge_player_tracks_file(
                    chart, out, target=target_v.get(), conflict=conflict_v.get(),
                    tolerance=tol, reverse=bool(rev_v.get()), log=w._slog)
                song, _ = fc.load_chart_info(out)
                st = fc.chart_stats(song, fc.detect_layout(song)["keys"])
                w._slog(f"合并后：玩家 {st['bf']}　对手 {st['opp']}")
                w.after(0, lambda: messagebox.showinfo(
                    "完成",
                    f"合并 {res['merged']} 个音符，合唱冲突 {res['conflicts']} 个已处理\n\n{out}",
                    parent=w))
            except Exception as e:
                w._slog("✘ 合并失败：" + str(e))
                w.after(0, lambda: messagebox.showerror("失败", str(e), parent=w))
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------ 玩家↔对手互换窗口
    def open_swap_window(self):
        w = tk.Toplevel(self.root)
        w.title("一人唱两：玩家↔对手音符互换")
        w.geometry("720x640")
        try:
            w.tk.call("wm", "iconphoto", w._w, self.root._w)
        except Exception:
            pass
        pad = {"padx": 8, "pady": 3}
        chart_v = tk.StringVar()
        out_v = tk.StringVar()
        info_v = tk.StringVar(value="尚未选择谱面")

        ttk.Label(w, text="用途：谱面里玩家和对手对唱 / 代唱（一人唱两角），"
                          "或 mustHitSection 标反了。\n"
                          "规则：原 BF 音符归对手、原对手音符归 BF（小节标记不动），"
                          "之后在主界面选「导出音符」即可只打其中一边。",
                  foreground="#888", wraplength=680, justify="left"
                  ).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 4))

        ttk.Label(w, text="① 选择 FNF 谱面 JSON：").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(w, textvariable=chart_v).grid(row=2, column=0, columnspan=2,
                                                sticky="ew", **pad)
        ttk.Button(w, text="浏览…",
                   command=lambda: self._pick_swap_chart(w, chart_v, out_v, info_v)
                   ).grid(row=2, column=2, **pad)
        ttk.Label(w, textvariable=info_v, foreground="#0a6", justify="left",
                  wraplength=680).grid(row=3, column=0, columnspan=3, sticky="w", padx=8)

        ttk.Label(w, text="② 输出谱面 JSON：").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(w, textvariable=out_v).grid(row=5, column=0, columnspan=2,
                                              sticky="ew", **pad)
        ttk.Button(w, text="另存为…",
                   command=lambda: self._pick_swap_out(w, out_v)).grid(row=5, column=2, **pad)

        ttk.Button(w, text="开始互换（玩家 ↔ 对手）", command=lambda: self._swap_worker(
            w, chart_v, out_v)).grid(row=6, column=0, columnspan=3, pady=8)

        lb = tk.Text(w, height=12, wrap="word", state="disabled",
                     background="#111", foreground="#ddd", font=("Consolas", 9))
        lb.grid(row=7, column=0, columnspan=3, sticky="nsew", padx=8, pady=4)
        w.rowconfigure(7, weight=1)
        w.columnconfigure(0, weight=1)

        def slog(m):
            lb.configure(state="normal")
            lb.insert("end", str(m) + "\n")
            lb.see("end")
            lb.configure(state="disabled")
            w.update_idletasks()

        w._slog = slog
        enable_drop(w, lambda files: self._swap_drop(w, chart_v, out_v, info_v, files))

    def _pick_swap_chart(self, w, chart_v, out_v, info_v):
        p = filedialog.askopenfilename(
            title="选择 FNF 谱面 JSON", parent=w,
            filetypes=[("FNF 谱面", "*.json"), ("所有文件", "*.*")])
        if not p:
            return
        chart_v.set(p)
        self._swap_show_info(p, chart_v, out_v, info_v, w)

    def _swap_show_info(self, p, chart_v, out_v, info_v, w):
        try:
            song, cinfo = fc.load_chart_info(p)
            layout = fc.detect_layout(song)
            keys = layout["keys"]
            st = fc.chart_stats(song, keys)
            engine = fc.ENGINE_CN.get(cinfo.get("engine", ""), "")
            info_v.set(f"引擎：{engine}　{keys}K/侧　"
                       f"BF 音符 {st['bf']}　对手音符 {st['opp']}（互换后两者对调）")
            for fx in cinfo.get("fixed", []):
                w._slog("🔧 旧版格式归一化：" + fx)
            for wn in cinfo.get("warnings", []):
                w._slog("⚠ " + wn)
            base = os.path.splitext(os.path.basename(p))[0]
            out_v.set(os.path.join(os.path.dirname(p), f"{base}-swapped.json"))
            w._slog("已载入谱面：" + p)
        except Exception as e:
            info_v.set("读取失败：" + str(e))
            messagebox.showerror("读取失败", str(e), parent=w)

    def _pick_swap_out(self, w, out_v):
        p = filedialog.asksaveasfilename(
            title="输出谱面 JSON", parent=w, defaultextension=".json",
            filetypes=[("FNF 谱面 JSON", "*.json")])
        if p:
            out_v.set(p)

    def _swap_drop(self, w, chart_v, out_v, info_v, files):
        for p in files:
            if p.lower().endswith(".json") and os.path.isfile(p):
                chart_v.set(p)
                self._swap_show_info(p, chart_v, out_v, info_v, w)
                return

    def _swap_worker(self, w, chart_v, out_v):
        chart = chart_v.get().strip()
        out = out_v.get().strip()
        if not chart or not os.path.isfile(chart):
            messagebox.showwarning("缺少谱面", "请先选择谱面 JSON", parent=w)
            return
        if not out:
            base = os.path.splitext(os.path.basename(chart))[0]
            out = os.path.join(os.path.dirname(chart), f"{base}-swapped.json")
            out_v.set(out)
        elif not out.lower().endswith(".json"):
            out += ".json"
            out_v.set(out)

        def run():
            try:
                res = fc.swap_sides_file(chart, out, log=w._slog)
                song, _ = fc.load_chart_info(out)
                st = fc.chart_stats(song, fc.detect_layout(song)["keys"])
                w._slog(f"互换后：BF {st['bf']}　对手 {st['opp']}")
                w.after(0, lambda: messagebox.showinfo(
                    "完成",
                    f"玩家↔对手互换完成：{res['swapped']} 个音符\n\n{out}",
                    parent=w))
            except Exception:
                w._slog(traceback.format_exc())
                w.after(0, lambda: messagebox.showerror(
                    "互换失败", str(sys.exc_info()[1]), parent=w))

        threading.Thread(target=run, daemon=True).start()

    # ---------------------------------------------------- OSU 包 → FNF 模组窗口
    def open_osumod_window(self, pack_path=""):
        """pack_path 非空 = 主窗口拖入的包/单谱/目录，打开即自动扫描。"""
        import fosu_osumod as om
        w = tk.Toplevel(self.root)
        w.title("OSU 模组包 → FNF 模组（.osz / .olz / .osu / .zip → Psych mods/）")
        w.geometry("820x720")
        try:
            w.tk.call("wm", "iconphoto", w._w, self.root._w)
        except Exception:
            pass
        pad = {"padx": 8, "pady": 3}
        pack_v = tk.StringVar()
        out_v = tk.StringVar()
        info_v = tk.StringVar(value="尚未选择包")
        offset_v = tk.DoubleVar(value=0.0)
        speed_v = tk.DoubleVar(value=2.4)
        week_v = tk.BooleanVar(value=True)
        voices_v = tk.BooleanVar(value=True)
        auto_v = tk.BooleanVar(value=True)     # 选包后自动开始（一键）
        mode_v = tk.StringVar(value="full")    # full=全模组 / chart=仅谱面
        scan_hold = {"scan": None}

        ttk.Label(w, text="① 选择 OSU 包（.osz / .olz / .osu 单谱面 / .zip / 已解压目录）：",
                  ).grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(w, textvariable=pack_v).grid(row=1, column=0, columnspan=2,
                                               sticky="ew", **pad)

        logbox = tk.Text(w, height=16, wrap="word", state="disabled",
                         background="#111", foreground="#ddd", font=("Consolas", 9))

        def olog(m):
            def _do():
                logbox.configure(state="normal")
                logbox.insert("end", str(m) + "\n")
                logbox.see("end")
                logbox.configure(state="disabled")
            try:
                w.after(0, _do)
            except Exception:
                pass
        w._olog = olog

        def do_scan(path):
            if not path:
                return
            pack_v.set(path)
            info_v.set("扫描中…")

            def run():
                try:
                    sc = om.osu_pack_scan(path, log=lambda s: None)
                    keep = {k: sc[k] for k in ("charts", "audio", "images")}
                    if sc.get("temp"):
                        import shutil as _sh
                        _sh.rmtree(sc["root"], ignore_errors=True)
                    scan_hold["scan"] = keep
                    lines = ["包内 %d 个谱面：" % len(keep["charts"])]
                    for c in keep["charts"]:
                        lines.append("  · %s / %s / %dK %s" % (
                            c["title"] or "?", c["version"] or "未命名",
                            c["keys"], c["mode"] == 3 and "mania" or "非mania"))
                    lines.append("音频 %d 个，封面 %d 张" % (len(keep["audio"]),
                                                          len(keep["images"])))
                    if not out_v.get():
                        base = os.path.splitext(os.path.basename(path.rstrip("/\\")))[0]
                        out_v.set(os.path.join(os.path.dirname(os.path.abspath(path)),
                                               base + "_FNFMod"))
                    w.after(0, lambda: info_v.set("\n".join(lines)))
                    # 一键：扫描完成即自动开转（勾着「选包后自动开始」时）
                    if auto_v.get() and out_v.get():
                        w.after(120, on_go)
                except Exception as e:
                    w.after(0, lambda: info_v.set("扫描失败：" + str(e)))
            threading.Thread(target=run, daemon=True).start()

        def pick_file():
            p = filedialog.askopenfilename(
                parent=w, title="选择 OSU 包",
                filetypes=[("OSU 包/谱面", "*.osz *.olz *.osu *.zip"),
                           ("osu! 谱面包", "*.osz"), ("osu!lazer 谱包", "*.olz"),
                           ("单谱面", "*.osu"), ("所有文件", "*.*")])
            if p:
                do_scan(p)

        def pick_dir():
            p = filedialog.askdirectory(parent=w, title="选择已解压的 osu 谱面目录")
            if p:
                do_scan(p)

        ttk.Button(w, text="选包…", command=pick_file).grid(row=1, column=2, **pad)
        ttk.Button(w, text="选目录…", command=pick_dir).grid(row=1, column=3, **pad)
        ttk.Label(w, textvariable=info_v, foreground="#0a6", justify="left",
                  wraplength=780).grid(row=2, column=0, columnspan=4, sticky="w", padx=8)

        ttk.Label(w, text="② 输出模组目录（会生成 data/ songs/ weeks/ 等，直接放进引擎 mods/）："
                  ).grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(w, textvariable=out_v).grid(row=4, column=0, columnspan=3,
                                              sticky="ew", **pad)

        def pick_out():
            p = filedialog.askdirectory(parent=w, title="选择模组输出目录")
            if p:
                out_v.set(os.path.join(p, os.path.basename(out_v.get()) or "NewMod"))
        ttk.Button(w, text="另存到…", command=pick_out).grid(row=4, column=3, **pad)

        row5 = ttk.Frame(w)
        row5.grid(row=6, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        ttk.Label(row5, text="时间偏移(ms)：").pack(side="left")
        ttk.Spinbox(row5, from_=-3000, to=3000, increment=10, width=7,
                    textvariable=offset_v).pack(side="left")
        ttk.Label(row5, text="  滚动速度：").pack(side="left")
        ttk.Spinbox(row5, from_=0.5, to=4.0, increment=0.1, width=5,
                    textvariable=speed_v).pack(side="left")

        row5b = ttk.LabelFrame(w, text="导出模式")
        row5b.grid(row=5, column=0, columnspan=4, sticky="ew", padx=8, pady=2)
        ttk.Radiobutton(row5b, text="导出全模组（谱面 + 音乐 + week + 封面 + meta，"
                                     "角色默认 BF / DAD / GF，舞台默认 stage）",
                        variable=mode_v, value="full").pack(side="left", padx=4)
        ttk.Radiobutton(row5b, text="只导出谱面（仅 data/ 里的 json）",
                        variable=mode_v, value="chart").pack(side="left", padx=12)

        row6 = ttk.Frame(w)
        row6.grid(row=7, column=0, columnspan=4, sticky="w", padx=8)
        ttk.Checkbutton(row6, text="生成 week json（Freeplay 可见）",
                        variable=week_v).pack(side="left")
        ttk.Checkbutton(row6, text="生成静音 Voices.ogg",
                        variable=voices_v).pack(side="left")
        ttk.Checkbutton(row6, text="选包后自动开始（一键）",
                        variable=auto_v).pack(side="left", padx=8)

        go = ttk.Button(w, text="开始转换（→ FNF 模组）")
        go.grid(row=8, column=0, columnspan=2, pady=8)

        def on_go():
            pack = pack_v.get().strip()
            out = out_v.get().strip()
            if not pack or not os.path.exists(pack):
                messagebox.showwarning("缺少输入", "请先选择 OSU 包", parent=w)
                return
            if not out:
                messagebox.showwarning("缺少输出", "请指定模组输出目录", parent=w)
                return
            if mode_v.get() == "full" and not ffmpeg_ready():
                messagebox.showerror(
                    "缺少 ffmpeg",
                    "全模组导出（音乐转 OGG/封面）需要 ffmpeg.exe。\n"
                    "可以把 ffmpeg.exe 放到本 exe 同目录，或加入系统 PATH；\n"
                    "也可以切换为「只导出谱面」跳过音频。",
                    parent=w)
                return
            go.configure(state="disabled")

            def run():
                try:
                    r = om.osu_pack_to_fnf_mod(
                        pack, out, offset_ms=float(offset_v.get()),
                        speed=float(speed_v.get()), make_week=bool(week_v.get()),
                        silent_voices=bool(voices_v.get()),
                        mode=mode_v.get(), log=olog)
                    w.after(0, lambda: done(True, r))
                except Exception:
                    olog(traceback.format_exc())
                    w.after(0, lambda: done(False, None))

            def done(ok, r):
                go.configure(state="normal")
                if ok:
                    kind = "FNF 模组" if mode_v.get() == "full" else "谱面"
                    olog("✔ %s 已生成：%s" % (kind, r["mod_root"]))
                    if messagebox.askyesno(
                            "完成",
                            "%s 已生成：\n%s\n\n难度：%s\n音符：%d\n\n"
                            "打开所在文件夹？" % (kind, r["mod_root"],
                                                 ','.join(r["difficulties"]),
                                                 r["notes"]), parent=w):
                        try:
                            os.startfile(r["mod_root"])
                        except Exception:
                            pass
                else:
                    messagebox.showerror("失败", "转换失败，详见日志", parent=w)

            threading.Thread(target=run, daemon=True).start()

        logbox.grid(row=9, column=0, columnspan=4, sticky="nsew", padx=8, pady=6)
        w.rowconfigure(9, weight=1)
        ttk.Label(w, text="提示：osu!mania 谱面才会转换（Mode: 3）；"
                          "多难度包会每个 .osu 生成一个难度，至少有一个叫 hard。",
                  foreground="#888").grid(row=10, column=0, columnspan=4,
                                          sticky="w", padx=8, pady=2)

        # v5.1：本窗口也支持直接拖入 .osz/.olz/.osu/目录；主窗口拖入则带参自动扫描
        def _drop(files):
            for _p in files:
                if os.path.exists(_p):
                    do_scan(_p)
                    break
        enable_drop(w, _drop)
        if pack_path:
            w.after(80, lambda: do_scan(pack_path))

    # ------------------------------------------------------------ OSU→FNF 窗口
    def open_osu2fnf_window(self, osu_path=""):
        """osu_path 非空 = 主窗口拖入的 .osu，打开即自动载入。"""
        w = tk.Toplevel(self.root)
        w.title("osu!mania 谱面 → FNF（含音乐转 OGG）")
        w.geometry("760x700")
        try:
            w.tk.call("wm", "iconphoto", w._w, self.root._w)
        except Exception:
            pass
        pad = {"padx": 8, "pady": 3}
        osu_v = tk.StringVar()
        audio_v = tk.StringVar()
        out_v = tk.StringVar()
        info_v = tk.StringVar(value="尚未选择 .osu 谱面")
        offset_v = tk.DoubleVar(value=0.0)
        vol_v = tk.DoubleVar(value=1.0)
        ogg_v = tk.BooleanVar(value=True)

        ttk.Label(w, text="① 选择 osu!mania 谱面（.osu，4K/7K 均可；hold 长按会自动转换）：",
                  ).grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(w, textvariable=osu_v).grid(row=1, column=0, columnspan=2,
                                              sticky="ew", **pad)
        ttk.Button(w, text="浏览…",
                   command=lambda: self._pick_osu_chart(w, osu_v, out_v, info_v)
                   ).grid(row=1, column=2, **pad)
        ttk.Label(w, textvariable=info_v, foreground="#0a6", justify="left",
                  wraplength=720).grid(row=2, column=0, columnspan=3, sticky="w", padx=8)

        ttk.Label(w, text="② 音乐（可选，转成 OGG 当 FNF 背景音乐）：").grid(
            row=3, column=0, sticky="w", **pad)
        ttk.Entry(w, textvariable=audio_v).grid(row=4, column=0, columnspan=2,
                                                sticky="ew", **pad)
        ttk.Button(w, text="浏览…",
                   command=lambda: self.pick_audio(audio_v)).grid(row=4, column=2, **pad)

        row5 = ttk.Frame(w)
        row5.grid(row=5, column=0, columnspan=3, sticky="w", padx=8)
        ttk.Checkbutton(row5, text="转成 Inst.ogg 放到谱面同目录",
                        variable=ogg_v).pack(side="left")
        ttk.Label(row5, text="  音量").pack(side="left")
        ttk.Spinbox(row5, from_=0, to=2, increment=0.05, width=5,
                    textvariable=vol_v).pack(side="left")

        ttk.Label(w, text="③ 时间偏移(ms)：").grid(row=6, column=0, sticky="w", **pad)
        ttk.Spinbox(w, from_=-5000, to=5000, increment=10, width=8,
                    textvariable=offset_v).grid(row=6, column=1, sticky="w", **pad)
        ttk.Label(w, text="正=延后、负=提前；osu 的 AudioLeadIn 会让开头空一段，"
                          "想抵消就填对应的负值", foreground="#888"
                  ).grid(row=6, column=2, sticky="w")

        ttk.Label(w, text="④ 输出 FNF 谱面 JSON：").grid(row=7, column=0, sticky="w", **pad)
        ttk.Entry(w, textvariable=out_v).grid(row=8, column=0, columnspan=2,
                                              sticky="ew", **pad)
        ttk.Button(w, text="另存为…",
                   command=lambda: self._pick_osu_out(w, out_v)).grid(row=8, column=2, **pad)

        go = ttk.Button(w, text="开始转换（→ FNF）", command=lambda: self._osu2fnf_worker(
            w, osu_v, audio_v, ogg_v, vol_v, offset_v, out_v, go))
        go.grid(row=9, column=0, columnspan=3, pady=8)

        lb = tk.Text(w, height=12, wrap="word", state="disabled",
                     background="#111", foreground="#ddd", font=("Consolas", 9))
        lb.grid(row=10, column=0, columnspan=3, sticky="nsew", padx=8, pady=4)
        w.rowconfigure(10, weight=1)
        w.columnconfigure(0, weight=1)

        def olog(m):
            lb.configure(state="normal")
            lb.insert("end", str(m) + "\n")
            lb.see("end")
            lb.configure(state="disabled")
            w.update_idletasks()

        w._olog = olog
        enable_drop(w, lambda files: self._osu2fnf_drop(
            w, osu_v, audio_v, out_v, info_v, files))
        if osu_path:
            # v5.1：主窗口拖入 .osu 时带参打开，直接走一遍载入逻辑
            w.after(80, lambda: self._osu2fnf_drop(
                w, osu_v, audio_v, out_v, info_v, [osu_path]))

    def _pick_osu_chart(self, w, osu_v, out_v, info_v):
        p = filedialog.askopenfilename(
            title="选择 osu!mania 谱面", parent=w,
            filetypes=[("osu! 谱面", "*.osu"), ("所有文件", "*.*")])
        if not p:
            return
        osu_v.set(p)
        try:
            m = fc._osu_parse(p)
            info_v.set(f"{m['keys']}K　BPM {m['bpm']:g}　音符 {len(m['notes'])} 个"
                       f"（长按 {sum(1 for n in m['notes'] if n['length'] > 0)}）　"
                       f"AudioLeadIn {m['lead_in']}ms　音频：{m['audio'] or '（未指定）'}")
            base = os.path.splitext(os.path.basename(p))[0]
            out_v.set(os.path.join(os.path.dirname(p), f"{base}-fnf.json"))
            w._olog("已载入：" + p)
        except Exception as e:
            info_v.set("读取失败：" + str(e))
            messagebox.showerror("读取失败", str(e), parent=w)

    def _pick_osu_out(self, w, out_v):
        p = filedialog.asksaveasfilename(
            title="输出 FNF 谱面 JSON", parent=w, defaultextension=".json",
            filetypes=[("FNF 谱面 JSON", "*.json")])
        if p:
            out_v.set(p)

    def _osu2fnf_drop(self, w, osu_v, audio_v, out_v, info_v, files):
        for p in files:
            if not os.path.isfile(p):
                continue
            low = p.lower()
            if low.endswith(".osu") and not osu_v.get():
                osu_v.set(p)
                try:
                    m = fc._osu_parse(p)
                    info_v.set(f"{m['keys']}K　BPM {m['bpm']:g}　音符 {len(m['notes'])} 个"
                               f"（长按 {sum(1 for n in m['notes'] if n['length'] > 0)}）　"
                               f"AudioLeadIn {m['lead_in']}ms　音频：{m['audio'] or '（未指定）'}")
                    base = os.path.splitext(os.path.basename(p))[0]
                    out_v.set(os.path.join(os.path.dirname(p), f"{base}-fnf.json"))
                    w._olog("已导入谱面：" + p)
                except Exception as e:
                    info_v.set("读取失败：" + str(e))
            elif low.endswith(AUDIO_EXTS) and not audio_v.get():
                audio_v.set(p)
                w._olog("已导入音频：" + p)

    def _osu2fnf_worker(self, w, osu_v, audio_v, ogg_v, vol_v, offset_v, out_v, go):
        osu = osu_v.get().strip()
        out = out_v.get().strip()
        if not osu or not os.path.isfile(osu):
            messagebox.showwarning("缺少谱面", "请先选择 .osu 谱面文件", parent=w)
            return
        if not out:
            base = os.path.splitext(os.path.basename(osu))[0]
            out = os.path.join(os.path.dirname(osu), f"{base}-fnf.json")
            out_v.set(out)
        elif not out.lower().endswith(".json"):
            out += ".json"
            out_v.set(out)
        audio = audio_v.get().strip()
        if audio and not os.path.isfile(audio):
            messagebox.showwarning("音频缺失", f"找不到音频文件：\n{audio}", parent=w)
            return
        want_ogg = ogg_v.get() and bool(audio)
        if want_ogg and not ffmpeg_ready():
            messagebox.showerror(
                "缺少 ffmpeg",
                "转 OGG 需要 ffmpeg.exe。\n把 ffmpeg.exe 放到本 exe 同目录，或加入系统 PATH。",
                parent=w)
            return
        go.configure(state="disabled")

        def run():
            try:
                r = fc.osu2fnf(osu, out, offset_ms=float(offset_v.get()),
                               log=w._olog)
                if r.get("audio") and not audio:
                    w._olog(f"⚠ .osu 声明音频 {r['audio']}，但未选择音频文件，"
                            f"只生成了谱面 JSON")
                if want_ogg:
                    fc.convert_audio_to_ogg(audio, os.path.dirname(os.path.abspath(out)),
                                            name="Inst.ogg",
                                            volume=float(vol_v.get()),
                                            ffmpeg=fc.find_ffmpeg(), log=w._olog)
                w.after(0, lambda: self._osu2fnf_done(w, go, True, out, r))
            except Exception:
                w._olog(traceback.format_exc())
                w.after(0, lambda: self._osu2fnf_done(w, go, False, out, None))

        threading.Thread(target=run, daemon=True).start()

    def _osu2fnf_done(self, w, go, ok, out, r):
        go.configure(state="normal")
        if ok:
            w._olog(f"✔ 完成：{out}")
            if messagebox.askyesno(
                    "完成",
                    f"FNF 谱面已生成：\n{out}\n\n"
                    f"{r['keys']}K / BPM {r['bpm']:g} / {r['notes']} 音符"
                    f"（长按 {r['holds']}）\n\n是否打开所在文件夹？", parent=w):
                try:
                    os.startfile(os.path.dirname(out))
                except Exception:
                    pass
        else:
            messagebox.showerror("转换失败", "详见日志", parent=w)

    # ------------------------------------------------------------ 键数互转窗口
    def open_keys_window(self):
        w = tk.Toplevel(self.root)
        w.title("FNF 键数互转（4K ↔ 多K）")
        w.geometry("680x560")
        try:
            w.tk.call("wm", "iconphoto", w._w, self.root._w)
        except Exception:
            pass
        pad = {"padx": 8, "pady": 3}
        chart_v = tk.StringVar()
        from_v = tk.StringVar(value="—")
        to_v = tk.IntVar(value=7)
        damage_v = tk.BooleanVar(value=True)
        out_v = tk.StringVar()
        info_v = tk.StringVar(value="尚未选择谱面")

        ttk.Label(w, text="① 选择 FNF 谱面 JSON：").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(w, textvariable=chart_v).grid(row=1, column=0, columnspan=2, sticky="ew", **pad)
        ttk.Button(w, text="浏览…",
                   command=lambda: self._pick_keys_chart(w, chart_v, from_v, out_v, info_v)
                   ).grid(row=1, column=2, **pad)
        ttk.Label(w, textvariable=info_v, foreground="#0a6", justify="left",
                  wraplength=640).grid(row=2, column=0, columnspan=3, sticky="w", padx=8)

        ttk.Label(w, text="② 目标键数（直接输入数字，如 7 / 9 / 4）：").grid(
            row=3, column=0, sticky="w", **pad)
        ttk.Spinbox(w, from_=1, to=32, increment=1, width=6,
                    textvariable=to_v).grid(row=3, column=1, sticky="w", **pad)
        ttk.Checkbutton(w, text="保留扣血音符（noteType 3）",
                        variable=damage_v).grid(row=3, column=2, sticky="w", padx=8)

        ttk.Label(w, text="③ 输出谱面 JSON：").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(w, textvariable=out_v).grid(row=5, column=0, columnspan=2, sticky="ew", **pad)
        ttk.Button(w, text="另存为…",
                   command=lambda: self._pick_keys_out(w, out_v)).grid(row=5, column=2, **pad)

        ttk.Button(w, text="开始转换", command=lambda: self._keys_worker(
            w, chart_v, from_v, to_v, damage_v, out_v)).grid(
            row=6, column=0, columnspan=3, pady=8)

        lb = tk.Text(w, height=11, wrap="word", state="disabled",
                     background="#111", foreground="#ddd", font=("Consolas", 9))
        lb.grid(row=7, column=0, columnspan=3, sticky="nsew", padx=8, pady=4)
        w.rowconfigure(7, weight=1)
        w.columnconfigure(0, weight=1)

        def klog(m):
            lb.configure(state="normal")
            lb.insert("end", str(m) + "\n")
            lb.see("end")
            lb.configure(state="disabled")
            w.update_idletasks()

        w._klog = klog
        enable_drop(w, lambda files: self._keys_drop(
            w, chart_v, from_v, out_v, info_v, files))

    def _pick_keys_chart(self, w, chart_v, from_v, out_v, info_v):
        p = filedialog.askopenfilename(
            title="选择 FNF 谱面 JSON",
            filetypes=[("FNF 谱面", "*.json"), ("所有文件", "*.*")])
        if not p:
            return
        chart_v.set(p)
        try:
            song = fc.load_chart(p)
            layout = fc.detect_layout(song)
            keys = layout["keys"]
            st = fc.chart_stats(song, keys)
            from_v.set(f"{keys}K")
            info_v.set(f"已载入：自动检测原谱面为 {keys}K/侧　"
                       f"BF {st['bf']}　对手 {st['opp']}　扣血 {st['damage']}")
            base = os.path.splitext(os.path.basename(p))[0]
            out_v.set(os.path.join(os.path.dirname(p), f"{base}-{keys}Kto7K.json"))
            w._klog("已载入谱面：" + p)
            for wn in layout["warnings"]:
                w._klog("⚠ " + wn)
        except Exception as e:
            info_v.set("读取失败：" + str(e))
            messagebox.showerror("读取失败", str(e))

    def _pick_keys_out(self, w, out_v):
        p = filedialog.asksaveasfilename(
            title="输出谱面 JSON", defaultextension=".json",
            filetypes=[("FNF 谱面 JSON", "*.json")])
        if p:
            out_v.set(p)

    def _keys_worker(self, w, chart_v, from_v, to_v, damage_v, out_v):
        chart = chart_v.get().strip()
        if not chart or not os.path.isfile(chart):
            messagebox.showwarning("缺少谱面", "请先选择谱面 JSON")
            return
        try:
            to_k = int(to_v.get())
        except Exception:
            messagebox.showwarning("键数无效", "目标键数必须是整数")
            return
        if to_k < 1:
            messagebox.showwarning("键数无效", "目标键数必须 >= 1")
            return
        out = out_v.get().strip()
        import re
        bn = os.path.basename(out)
        if re.search(r'-\d+Kto\d+K\.json$', bn):
            # 默认命名模式：跟随目标键数自动更新文件名
            stem = os.path.splitext(bn)[0]
            pre = stem.rsplit('-', 1)[0]
            out = os.path.join(os.path.dirname(out), f"{pre}-{to_k}K.json")
            out_v.set(out)
        elif not out:
            base = os.path.splitext(os.path.basename(chart))[0]
            out = os.path.join(os.path.dirname(chart), f"{base}-{to_k}K.json")
            out_v.set(out)
        elif not out.lower().endswith(".json"):
            out += ".json"
            out_v.set(out)

        def run():
            try:
                res = fc.convert_keys_file(
                    chart, out, to_k, from_keys=0,
                    keep_damage=damage_v.get(), log=w._klog)
                w._klog(f"✔ 完成：{res['from']}K → {res['to']}K，"
                        f"转换 {res['converted']} 个音符 -> {out}")
                messagebox.showinfo(
                    "完成",
                    f"键数转换完成：{res['from']}K → {res['to']}K\n"
                    f"{res['converted']} 个音符\n\n{out}")
            except Exception:
                w._klog(traceback.format_exc())
                messagebox.showerror("转换失败", str(sys.exc_info()[1]))

        threading.Thread(target=run, daemon=True).start()

    def _keys_drop(self, w, chart_v, from_v, out_v, info_v, files):
        for p in files:
            if p.lower().endswith(".json") and os.path.isfile(p):
                chart_v.set(p)
                try:
                    song = fc.load_chart(p)
                    layout = fc.detect_layout(song)
                    keys = layout["keys"]
                    st = fc.chart_stats(song, keys)
                    from_v.set(f"{keys}K")
                    info_v.set(f"已载入：自动检测原谱面为 {keys}K/侧　"
                               f"BF {st['bf']}　对手 {st['opp']}　扣血 {st['damage']}")
                    base = os.path.splitext(os.path.basename(p))[0]
                    out_v.set(os.path.join(os.path.dirname(p), f"{base}-{keys}Kto7K.json"))
                    w._klog("已载入谱面：" + p)
                except Exception as e:
                    info_v.set("读取失败：" + str(e))
                return

    # ------------------------------------------------------------ 多难度批量打包窗口
    def _multi_default_opts(self, chart):
        return fc.ConvertOptions(
            side="bf", filter_damage=self.damage_var.get(), keys_override=0,
            version=guess_version(chart), hp=float(self.hp_var.get()),
            od=float(self.od_var.get()), slider_multiplier=float(self.sm_var.get()),
            offset_ms=float(self.offset_var.get()),
            artist=self.artist_var.get().strip() or "FNF", creator=APP_NAME)

    def _multi_refresh(self, tree, entries):
        tree.delete(*tree.get_children())
        side_cn = {"bf": "BF（玩家）", "opp": "对手", "all": "全部"}
        for i, e in enumerate(entries):
            o = e["opts"]
            ks = "自动" if not o.keys_override else str(o.keys_override)
            tree.insert("", "end", iid=str(i),
                        values=(o.version, ks, side_cn.get(o.side, o.side), e["chart"]))

    def open_multi_window(self):
        w = tk.Toplevel(self.root)
        w.title("多难度批量打包（多个难度 → 一个 .osz）")
        w.geometry("800x680")
        try:
            w.tk.call("wm", "iconphoto", w._w, self.root._w)
        except Exception:
            pass
        pad = {"padx": 8, "pady": 3}

        inst_v = tk.StringVar(value=self.inst_var.get())
        voices_v = tk.StringVar(value=self.voices_var.get())
        video_v = tk.StringVar(value=self.video_var.get())
        out_v = tk.StringVar()
        entries = []   # [{"chart": 路径, "opts": ConvertOptions}]

        ttk.Label(w, text="共享音频 / 视频（整个谱面集共用一份，已从主界面带入）").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 2))
        for r, (lab, var, kind) in enumerate(
                [("Inst：", inst_v, "audio"), ("Voices：", voices_v, "audio"),
                 ("视频：", video_v, "video")], start=1):
            ttk.Label(w, text=lab).grid(row=r, column=0, sticky="w", **pad)
            ttk.Entry(w, textvariable=var).grid(row=r, column=1, columnspan=2,
                                                sticky="ew", **pad)
            ttk.Button(w, text="浏览…",
                       command=lambda v=var, k=kind: self._multi_pick_media(w, v, k)
                       ).grid(row=r, column=3, **pad)

        ttk.Label(w, text="难度列表（逐个添加；每项可自定义难度名 / 键数 / 导出侧）").grid(
            row=4, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 2))
        tree = ttk.Treeview(w, columns=("diff", "keys", "side", "chart"),
                            show="headings", height=9)
        for c, t, wd in (("diff", "难度名", 120), ("keys", "键数", 55),
                         ("side", "导出侧", 90), ("chart", "谱面 JSON", 340)):
            tree.heading(c, text=t)
            tree.column(c, width=wd, anchor="w")
        tree.grid(row=5, column=0, columnspan=4, sticky="nsew", padx=8, pady=4)
        tree.bind("<Double-1>", lambda e: self._multi_edit(w, tree, entries))
        tree.bind("<Delete>", lambda e: self._multi_del(tree, entries))

        btns = ttk.Frame(w)
        btns.grid(row=6, column=0, columnspan=4, sticky="w", padx=8)
        ttk.Button(btns, text="＋ 添加难度…",
                   command=lambda: self._multi_add(w, tree, entries)).pack(side="left")
        ttk.Button(btns, text="编辑所选",
                   command=lambda: self._multi_edit(w, tree, entries)).pack(side="left", padx=6)
        ttk.Button(btns, text="移除所选",
                   command=lambda: self._multi_del(tree, entries)).pack(side="left")
        ttk.Button(btns, text="清空",
                   command=lambda: self._multi_clear(tree, entries)).pack(side="left", padx=6)

        ttk.Label(w, text="输出：").grid(row=7, column=0, sticky="w", **pad)
        ttk.Entry(w, textvariable=out_v).grid(row=7, column=1, columnspan=2,
                                              sticky="ew", **pad)
        ttk.Button(w, text="另存为…",
                   command=lambda: self._multi_out(w, out_v, entries)).grid(row=7, column=3, **pad)

        go = ttk.Button(w, text="开始打包（→ 一个 osz）",
                        command=lambda: self._multi_start(
                            w, tree, entries, inst_v, voices_v, video_v, out_v, go))
        go.grid(row=8, column=0, columnspan=4, pady=8)

        lb = tk.Text(w, height=10, wrap="word", state="disabled",
                     background="#111", foreground="#ddd", font=("Consolas", 9))
        lb.grid(row=9, column=0, columnspan=4, sticky="nsew", padx=8, pady=4)

        def mlog(m):
            lb.configure(state="normal")
            lb.insert("end", str(m) + "\n")
            lb.see("end")
            lb.configure(state="disabled")
            w.update_idletasks()

        w._mlog = mlog
        w.rowconfigure(5, weight=1)
        w.rowconfigure(9, weight=1)
        w.columnconfigure(1, weight=1)
        enable_drop(w, lambda files: self._multi_drop(w, tree, entries, files))

    def _multi_pick_media(self, w, var, kind):
        if kind == "audio":
            p = filedialog.askopenfilename(
                title="选择音频", parent=w,
                filetypes=[("音频", "*.ogg *.mp3 *.wav"), ("所有文件", "*.*")])
        else:
            p = filedialog.askopenfilename(
                title="选择视频（可选）", parent=w,
                filetypes=[("视频", "*.mp4 *.mov *.mkv *.avi *.flv *.webm *.wmv"),
                           ("所有文件", "*.*")])
        if p:
            var.set(p)

    def _multi_add(self, w, tree, entries):
        p = filedialog.askopenfilename(
            title="选择 FNF 谱面 JSON", parent=w,
            filetypes=[("FNF 谱面", "*.json"), ("所有文件", "*.*")])
        if not p:
            return
        try:
            fc.load_chart(p)
        except Exception as e:
            messagebox.showerror("读取失败", str(e), parent=w)
            return
        self._multi_edit_dialog(w, tree, entries, p, None)

    def _multi_edit(self, w, tree, entries):
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("未选择", "请先在列表里选一项", parent=w)
            return
        idx = int(sel[0])
        self._multi_edit_dialog(w, tree, entries, entries[idx]["chart"], idx)

    def _multi_edit_dialog(self, w, tree, entries, chart, idx):
        d = tk.Toplevel(w)
        d.title("编辑难度" if idx is not None else "添加难度")
        d.transient(w)
        d.grab_set()
        d.resizable(False, False)
        pad = {"padx": 8, "pady": 4}
        ttk.Label(d, text="谱面：").grid(row=0, column=0, sticky="w", **pad)
        ttk.Label(d, text=os.path.basename(chart)).grid(
            row=0, column=1, columnspan=2, sticky="w", **pad)
        cur = entries[idx]["opts"] if idx is not None else None
        diff_v = tk.StringVar(value=(cur.version if cur else guess_version(chart)))
        keys_v = tk.StringVar(value=("自动" if not cur or not cur.keys_override
                                     else str(cur.keys_override)))
        side_cn = {"bf": "BF（玩家）", "opp": "对手", "all": "全部"}
        side_v = tk.StringVar(value=(side_cn.get(cur.side, "BF（玩家）") if cur
                                     else "BF（玩家）"))
        dmg_v = tk.BooleanVar(value=(self.damage_var.get() if not cur
                                     else cur.filter_damage))
        ttk.Label(d, text="难度名：").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(d, textvariable=diff_v, width=22).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(d, text="键数：").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(d, textvariable=keys_v, width=8, state="readonly",
                     values=["自动", "4", "5", "6", "7", "8", "9"]).grid(
            row=2, column=1, sticky="w", **pad)
        ttk.Label(d, text="导出侧：").grid(row=3, column=0, sticky="w", **pad)
        ttk.Combobox(d, textvariable=side_v, width=12, state="readonly",
                     values=["BF（玩家）", "对手", "全部"]).grid(
            row=3, column=1, sticky="w", **pad)
        ttk.Checkbutton(d, text="过滤扣血音符", variable=dmg_v).grid(
            row=4, column=1, sticky="w", **pad)

        def ok():
            name = diff_v.get().strip() or "Normal"
            keys_o = 0 if keys_v.get() == "自动" else int(keys_v.get())
            side = {"BF（玩家）": "bf", "对手": "opp", "全部": "all"}.get(side_v.get(), "bf")
            opts = fc.ConvertOptions(
                side=side, filter_damage=dmg_v.get(), keys_override=keys_o,
                version=name, hp=float(self.hp_var.get()), od=float(self.od_var.get()),
                slider_multiplier=float(self.sm_var.get()),
                offset_ms=float(self.offset_var.get()),
                artist=self.artist_var.get().strip() or "FNF", creator=APP_NAME)
            if idx is None:
                entries.append({"chart": chart, "opts": opts})
            else:
                entries[idx] = {"chart": chart, "opts": opts}
            self._multi_refresh(tree, entries)
            d.destroy()

        ttk.Button(d, text="确定", command=ok).grid(row=5, column=1, sticky="e", pady=6)
        ttk.Button(d, text="取消", command=d.destroy).grid(row=5, column=2, sticky="w", pady=6)
        d.wait_window()

    def _multi_del(self, tree, entries):
        sel = tree.selection()
        if not sel:
            return
        for iid in sorted((int(s) for s in sel), reverse=True):
            entries.pop(iid)
        self._multi_refresh(tree, entries)

    def _multi_clear(self, tree, entries):
        entries.clear()
        self._multi_refresh(tree, entries)

    def _multi_drop(self, w, tree, entries, files):
        n = 0
        for p in files:
            if p.lower().endswith(".json") and os.path.isfile(p):
                try:
                    fc.load_chart(p)
                except Exception:
                    continue
                entries.append({"chart": p, "opts": self._multi_default_opts(p)})
                n += 1
        if n:
            self._multi_refresh(tree, entries)
            w._mlog(f"拖入添加 {n} 个难度")

    def _multi_out(self, w, out_v, entries):
        init = "fosu-multi.osz"
        if entries:
            try:
                song = fc.load_chart(entries[0]["chart"])
                t = fc.prettify(str(song.get("song", "fosu-multi")))
                init = fc.safe_name(f"{self.artist_var.get()} - {t} (fosu).osz")
            except Exception:
                pass
        p = filedialog.asksaveasfilename(
            title="输出 osu! 谱面包", parent=w, initialfile=init,
            defaultextension=".osz", filetypes=[("osu! 谱面包", "*.osz")])
        if p:
            out_v.set(p)

    def _multi_start(self, w, tree, entries, inst_v, voices_v, video_v, out_v, go):
        if not entries:
            messagebox.showwarning("没有难度", "请先添加至少一个难度", parent=w)
            return
        out = out_v.get().strip()
        if not out:
            messagebox.showwarning("缺少输出路径", "请指定输出的 .osz 路径", parent=w)
            return
        if not out.lower().endswith(".osz"):
            out += ".osz"
            out_v.set(out)
        inst = inst_v.get().strip()
        voices = voices_v.get().strip() if self.mix_var.get() else ""
        video = video_v.get().strip()
        for p, lab in ((inst, "Inst"), (voices, "Voices"), (video, "视频")):
            if p and not os.path.isfile(p):
                messagebox.showwarning(f"{lab} 缺失", f"找不到：\n{p}", parent=w)
                return
        if not (inst or voices or video):
            # v5.1：与主窗口一致——无音频也可导出（无声 osz），不再硬拦
            if not messagebox.askyesno(
                    "没有音频",
                    "没检测到 Inst / Voices / 视频音轨。\n\n"
                    "继续导出会得到无声的 osz（谱面正常）。现在继续吗？", parent=w):
                return
        if not ffmpeg_ready():
            messagebox.showerror(
                "缺少 ffmpeg",
                "找不到 ffmpeg.exe，无法处理音频/视频。\n"
                "把 ffmpeg.exe 放到本 exe 同目录，或加入系统 PATH。", parent=w)
            return

        vname = fc.safe_name(os.path.basename(video)) if video else ""
        for e in entries:
            e["opts"].video_path = video
            e["opts"].video_name = vname
        go.configure(state="disabled")

        def run():
            try:
                info = fc.convert_multi(
                    entries, out, inst_path=inst or None, voices_path=voices or None,
                    mix=bool(inst), vol_inst=float(self.inst_vol.get()),
                    vol_voices=float(self.voices_vol.get()),
                    video_path=video, video_name=vname,
                    ffmpeg=fc.find_ffmpeg(), log=w._mlog)
                w.after(0, lambda: self._multi_done(w, go, True, out, info))
            except Exception:
                tb = traceback.format_exc()
                w.after(0, w._mlog, tb)
                w.after(0, lambda: self._multi_done(w, go, False, out, tb))

        threading.Thread(target=run, daemon=True).start()

    def _multi_done(self, w, go, ok, out, info):
        go.configure(state="normal")
        if ok:
            w._mlog(f"✔ 完成：{out}")
            if messagebox.askyesno(
                    "完成",
                    f"谱面集已生成（{info.get('exported')} 个难度）：\n{out}\n\n"
                    "是否打开所在文件夹？", parent=w):
                try:
                    os.startfile(os.path.dirname(out))
                except Exception:
                    pass
        else:
            messagebox.showerror("打包失败", str(info), parent=w)


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        # windowed 打包后无控制台，日志写到输出文件旁
        chart, inst, voices, out = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
        log_path = out + ".selftest.log"
        with open(log_path, "w", encoding="utf-8") as lf:
            def wlog(m):
                lf.write(str(m) + "\n")
                lf.flush()
            try:
                wlog("frozen=%s meipass=%s" % (
                    getattr(sys, "frozen", False), getattr(sys, "_MEIPASS", "")))
                wlog("ffmpeg=%s" % fc.find_ffmpeg())
                run_headless(chart, inst, voices, out, log=wlog)
                wlog("SELFTEST OK -> " + out)
            except Exception:
                wlog(traceback.format_exc())
                raise
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "--keys":
        to_k = int(sys.argv[2])
        rest = sys.argv[3:]
        chart = rest[0] if rest else ""
        out = None
        from_k = 0
        keep = True
        i = 1
        while i < len(rest):
            a = rest[i]
            if a == "--from" and i + 1 < len(rest):
                from_k = int(rest[i + 1]); i += 2; continue
            if a == "--no-damage":
                keep = False; i += 1; continue
            if out is None and not a.startswith("--"):
                out = a; i += 1; continue
            i += 1
        if not chart or not os.path.isfile(chart):
            print("用法: fosu --keys <目标K> <谱面.json> [输出.json] [--from <原K>] [--no-damage]")
            return
        if not out:
            out = f"{os.path.splitext(chart)[0]}-{to_k}K.json"
        frozen = getattr(sys, "frozen", False)
        if frozen:
            lf = open(out + ".keys.log", "w", encoding="utf-8")

            def log(m):
                lf.write(str(m) + "\n"); lf.flush()
        else:
            def log(m):
                print(m)
        try:
            res = fc.convert_keys_file(chart, out, to_k, from_k, keep, log)
            log(f"完成：{res['from']}K → {res['to']}K，"
                f"{res['converted']} 个音符 -> {out}")
        except Exception:
            log(traceback.format_exc())
        if frozen:
            lf.close()
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "--swap":
        chart = sys.argv[2]
        out = sys.argv[3] if len(sys.argv) > 3 else \
            f"{os.path.splitext(chart)[0]}-swapped.json"
        try:
            res = fc.swap_sides_file(chart, out)
            print(f"完成：{res['swapped']} 个音符已互换 -> {out}")
        except Exception:
            print(traceback.format_exc())
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "--merge":
        chart = sys.argv[2]
        rest = sys.argv[3:]
        out = None
        target, conflict, tol, reverse = "player", "other_side", 10.0, False
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--target" and i + 1 < len(rest):
                target = rest[i + 1]; i += 2; continue
            if a == "--conflict" and i + 1 < len(rest):
                conflict = rest[i + 1]; i += 2; continue
            if a == "--tolerance" and i + 1 < len(rest):
                tol = float(rest[i + 1]); i += 2; continue
            if a == "--reverse":
                reverse = True; i += 1; continue
            if out is None and not a.startswith("--"):
                out = a; i += 1; continue
            i += 1
        if not out:
            out = os.path.splitext(chart)[0] + "-merged.json"
        try:
            r = fc.merge_player_tracks_file(chart, out, target=target,
                                            conflict=conflict, tolerance=tol,
                                            reverse=reverse)
            print(f"完成：合并 {r['merged']} / 冲突 {r['conflicts']} -> {out}")
        except Exception:
            print(traceback.format_exc())
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "--osu2mod":
        import fosu_osumod as _om
        pack = sys.argv[2]
        rest = sys.argv[3:]
        out_dir = ""
        offset = 0.0
        speed = 2.4
        make_week = True
        chart_only = False
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--offset" and i + 1 < len(rest):
                offset = float(rest[i + 1]); i += 2; continue
            if a == "--speed" and i + 1 < len(rest):
                speed = float(rest[i + 1]); i += 2; continue
            if a == "--no-week":
                make_week = False; i += 1; continue
            if a == "--chart-only":
                chart_only = True; i += 1; continue
            if not out_dir and not a.startswith("--"):
                out_dir = a; i += 1; continue
            i += 1
        if not out_dir:
            out_dir = os.path.join(os.path.dirname(os.path.abspath(pack)),
                                   os.path.splitext(os.path.basename(pack))[0] + "_FNFMod")
        frozen = getattr(sys, "frozen", False)
        lf = None
        if frozen:
            lf = open(os.path.join(os.getcwd(), "fosu_osu2mod.log"), "w", encoding="utf-8")

            def _log(m):
                lf.write(str(m) + "\n"); lf.flush()
        else:
            def _log(m):
                print(m)
        try:
            r = _om.osu_pack_to_fnf_mod(pack, out_dir, offset_ms=offset, speed=speed,
                                        make_week=make_week,
                                        mode="chart" if chart_only else "full",
                                        log=_log)
            _log("完成：%s -> %s（难度 %s，音符 %d）" % (
                r["title"], r["mod_root"], ','.join(r["difficulties"]), r["notes"]))
        except Exception:
            _log(traceback.format_exc())
        if lf:
            lf.close()
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "--osu2fnf":
        osu = sys.argv[2]
        rest = sys.argv[3:]
        out = None
        offset = 0.0
        audio = ""
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--offset" and i + 1 < len(rest):
                offset = float(rest[i + 1]); i += 2; continue
            if a == "--audio" and i + 1 < len(rest):
                audio = rest[i + 1]; i += 2; continue
            if out is None and not a.startswith("--"):
                out = a; i += 1; continue
            i += 1
        if not out:
            out = f"{os.path.splitext(osu)[0]}-fnf.json"
        try:
            r = fc.osu2fnf(osu, out, offset_ms=offset)
            print(f"完成：{r['keys']}K / {r['notes']} 音符 -> {out}")
            if audio:
                fc.convert_audio_to_ogg(audio, os.path.dirname(os.path.abspath(out)))
        except Exception:
            print(traceback.format_exc())
        return
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    gui = FosuGUI(root)
    # 主窗口拖拽导入（窗口映射后再挂 WM_DROPFILES 最稳）
    root.after(200, lambda: enable_drop(root, gui.on_drop_files))
    root.mainloop()


if __name__ == "__main__":
    main()
