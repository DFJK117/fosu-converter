# -*- coding: utf-8 -*-
"""
fosu_editor - FNF 谱面编辑器（Psych Engine 格式兼容 · 全汉化）

功能：
  · 打开 / 保存 PE 格式谱面 JSON（保持原始外壳与事件格式）
  · 竖向 note 预览区（参照原版编谱器）+ 下方网格时间轴
  · 键盘直接输入 note（默认 D F J K，可改键位）
  · 边播放边输入（播放头跟随，按键落 note，自动网格校正/量化）
  · 网格密度切换（1/4、1/8、1/16、1/32 拍）与缩放（Ctrl+滚轮 / +/-）
  · 特殊事件面板（相机/角色/声音等，两种事件格式自适应）
  · 导入音频真实播放（Inst+Voices 混音 / 波形显示 / 播放头跟音频零漂移）
"""
import bisect
import json
import math
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import fosu_audio

# FNF 方向：0=左 1=下 2=上 3=右
DIR_CN = ["左", "下", "上", "右"]
DEFAULT_BINDS = {"d": 0, "f": 1, "j": 2, "k": 3}

EVENT_TYPES_PE = ["Camera Speed", "Camera Cut", "Camera Zoom", "Character Change", "Sound"]
EVENT_TYPES_FREE = ["Camera Speed", "Character Change", "Sound", "forcecamtarget", "Legacy"]

ZOOM_MIN, ZOOM_MAX = 0.25, 6.0
GRID_DENSITIES = [4, 8, 16, 32]      # 每拍细分格数


# ---------------------------------------------------------------- 谱面数据模型

class ChartDoc:
    """封装一份 PE 谱面：保持原始结构，读写都尽量不破坏原格式。"""

    def __init__(self):
        self.data = {}          # 完整 JSON
        self.song = {}          # song 对象
        self.path = ""
        self.event_fmt = "array"    # "array" = [t, [[type,v1,v2]]] / "dict" = PE dict 式
        self.lane_style = "int"     # "int" = [t, lane, len] / "list" = [t, [lanes], len]
        self._notes_cache = None

    # ---------------- 载入 / 保存 ----------------
    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        doc = cls()
        doc.data = data
        doc.path = path
        if "song" in data and isinstance(data["song"], dict) and "notes" in data["song"]:
            doc.song = data["song"]
        else:
            doc.song = data
        doc._detect_formats()
        return doc

    def _detect_formats(self):
        ev = self.song.get("events")
        if isinstance(ev, list) and ev and isinstance(ev[0], dict):
            self.event_fmt = "dict"
        else:
            self.event_fmt = "array"
        # lane 风格：看第一个 note 的 noteData 是整数还是数组
        self.lane_style = "int"
        for sec in self.song.get("notes", []):
            for n in sec.get("sectionNotes", []):
                if len(n) >= 2:
                    self.lane_style = "list" if isinstance(n[1], list) else "int"
                    return

    def save(self, path=None):
        path = path or self.path
        if not path:
            raise ValueError("没有输出路径")
        self._write_events()
        self._write_lanes()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        self.path = path
        return path

    def _write_events(self):
        """事件按原格式写回。"""
        if self.event_fmt == "dict":
            out = []
            for e in self.song.get("events", []):
                if isinstance(e, dict):
                    out.append(e)
                elif isinstance(e, list) and len(e) >= 2:
                    for sub in e[1]:
                        if isinstance(sub, list) and len(sub) >= 3:
                            out.append({"strumTime": e[0], "event": sub[0],
                                        "value1": sub[1], "value2": sub[2]})
            self.song["events"] = out
        else:
            out = []
            for e in self.song.get("events", []):
                if isinstance(e, list) and len(e) >= 2 and isinstance(e[1], list):
                    subs = []
                    for sub in e[1]:
                        if isinstance(sub, list) and len(sub) >= 3:
                            subs.append([sub[0], sub[1], sub[2]])
                    if subs:
                        out.append([e[0], subs])
                elif isinstance(e, dict):
                    out.append([e.get("strumTime", 0),
                                [[e.get("event", ""), e.get("value1", ""),
                                  e.get("value2", "")]]])
            self.song["events"] = out

    def _write_lanes(self):
        """noteData 按原风格写回。"""
        if self.lane_style != "int":
            return
        for sec in self.song.get("notes", []):
            for n in sec.get("sectionNotes", []):
                if len(n) >= 2 and isinstance(n[1], list) and len(n[1]) == 1:
                    n[1] = n[1][0]

    # ---------------- 基本属性 ----------------
    @property
    def sections(self):
        return self.song.get("notes", [])

    @property
    def events(self):
        return self.song.get("events", [])

    @property
    def bpm(self):
        try:
            return float(self.song.get("bpm", 100) or 100)
        except (TypeError, ValueError):
            return 100.0

    @property
    def speed(self):
        try:
            return float(self.song.get("speed", 2.0) or 2.0)
        except (TypeError, ValueError):
            return 2.0

    @property
    def keys(self):
        """每侧键数（支持 PE 的 keys 字段 / mania 字段 / lanes 字段）。"""
        k = self.song.get("keys")
        if isinstance(k, int) and k > 0:
            return k
        m = self.song.get("mania")
        if isinstance(m, int) and m >= 0:
            return m + 1
        lanes = self.song.get("lanes")
        if isinstance(lanes, list) and lanes:
            return len(lanes)
        return 4

    @property
    def title(self):
        return str(self.song.get("song", os.path.basename(self.path) or "未命名"))

    # ---------------- 时间轴 ----------------
    def bpm_points(self):
        """返回 [(时间ms, bpm)]，含 changeBPM 段。"""
        base = self.bpm
        pts = [(0.0, base)]
        t = 0.0
        cur = base
        for sec in self.sections:
            if sec.get("changeBPM") and sec.get("bpm"):
                nb = float(sec["bpm"])
                if abs(nb - cur) > 1e-6:
                    pts.append((t, nb))
                    cur = nb
            steps = sec.get("lengthInSteps", 16) or 16
            t += (60000.0 / cur) * (steps / 4.0)
        return pts

    def bpm_at(self, t):
        cur = self.bpm
        for pt, b in self.bpm_points():
            if pt <= t:
                cur = b
            else:
                break
        return cur

    def beat_ms_at(self, t):
        return 60000.0 / self.bpm_at(t)

    def quantize(self, t, div=16):
        """把时间吸附到最近的网格点（div = 每拍格数）。"""
        step = self.beat_ms_at(t) / div
        if step <= 0:
            return t
        return round(t / step) * step

    def section_index_at(self, t):
        """时间落在第几个小节（按累加小节长度）。"""
        acc = 0.0
        for i, sec in enumerate(self.sections):
            steps = sec.get("lengthInSteps", 16) or 16
            length = (60000.0 / self.bpm_at(acc)) * (steps / 4.0)
            if acc <= t < acc + length:
                return i
            acc += length
        return max(0, len(self.sections) - 1)

    def section_start_time(self, idx):
        acc = 0.0
        for i, sec in enumerate(self.sections):
            if i == idx:
                return acc
            steps = sec.get("lengthInSteps", 16) or 16
            acc += (60000.0 / self.bpm_at(acc)) * (steps / 4.0)
        return acc

    def last_time(self):
        t = 0.0
        for sec in self.sections:
            for n in sec.get("sectionNotes", []):
                if len(n) >= 1:
                    try:
                        t = max(t, float(n[0]))
                    except (TypeError, ValueError):
                        pass
        return t

    # ---------------- note 操作 ----------------
    def _ensure_sections(self, count):
        notes = self.song.setdefault("notes", [])
        while len(notes) < count:
            notes.append({"sectionNotes": [], "mustHitSection": True,
                          "lengthInSteps": 16})

    def all_notes(self):
        """返回 [(sec_idx, note_dict)]，note_dict 规范化成 dict（按时间排序，带缓存）。"""
        if self._notes_cache is not None:
            return self._notes_cache
        out = []
        for si, sec in enumerate(self.sections):
            for n in sec.get("sectionNotes", []):
                if len(n) < 2:
                    continue
                try:
                    t = float(n[0])
                except (TypeError, ValueError):
                    continue
                lanes = n[1] if isinstance(n[1], list) else [n[1]]
                lanes = [int(x) for x in lanes if isinstance(x, (int, float))]
                length = float(n[2]) if len(n) >= 3 and n[2] else 0.0
                ntype = n[3] if len(n) >= 4 else None
                out.append({"sec": si, "raw": n, "time": t, "lanes": lanes,
                            "length": length, "type": ntype,
                            "must_hit": bool(sec.get("mustHitSection", False))})
        out.sort(key=lambda x: x["time"])
        self._notes_cache = out
        return out

    def invalidate(self):
        self._notes_cache = None

    def add_note(self, t, lane, length=0.0, must_hit=True):
        """在时间 t、方向 lane 加 note。"""
        t = round(t, 3)
        sec_idx = self.section_index_at(t)
        self._ensure_sections(sec_idx + 1)
        sec = self.sections[sec_idx]
        sec["mustHitSection"] = bool(must_hit)
        raw = [t, [int(lane)], round(float(length), 3)]
        sec.setdefault("sectionNotes", []).append(raw)
        self.invalidate()
        return raw

    def remove_note(self, note):
        sec = self.sections[note["sec"]]
        try:
            sec["sectionNotes"].remove(note["raw"])
            self.invalidate()
            return True
        except ValueError:
            return False

    def set_note_length(self, note, length):
        note["raw"][2] = round(float(length), 3)
        note["length"] = float(length)

    # ---------------- 事件操作 ----------------
    def event_rows(self):
        """规范化事件为 [(time, type, v1, v2, 原始元素)]。"""
        rows = []
        for e in self.events:
            if isinstance(e, dict):
                rows.append((float(e.get("strumTime", 0) or 0), e.get("event", ""),
                             e.get("value1", ""), e.get("value2", ""), e))
            elif isinstance(e, list) and len(e) >= 2 and isinstance(e[1], list):
                for sub in e[1]:
                    if isinstance(sub, list) and len(sub) >= 3:
                        rows.append((float(e[0]), sub[0], sub[1], sub[2], e))
        return rows

    def add_event(self, t, etype, v1="", v2=""):
        t = round(float(t), 3)
        if self.event_fmt == "dict":
            self.song.setdefault("events", []).append(
                {"strumTime": t, "event": etype, "value1": v1, "value2": v2})
        else:
            self.song.setdefault("events", []).append([t, [[etype, v1, v2]]])

    def remove_event(self, row):
        """删除一行事件（数组式按子项精确删，dict 式整条删）。"""
        t, etype, v1, v2, raw = row
        if isinstance(raw, dict):
            try:
                self.events.remove(raw)
                return True
            except ValueError:
                return False
        for i, e in enumerate(self.events):
            if e is raw and isinstance(e, list) and len(e) >= 2:
                subs = [s for s in e[1] if not (isinstance(s, list) and len(s) >= 3
                                               and s[0] == etype and s[1] == v1
                                               and s[2] == v2)]
                if subs:
                    e[1] = subs
                else:
                    self.events.pop(i)
                return True
        return False


# ---------------------------------------------------------------- 编辑器窗口

class EditorWindow:
    """FNF 谱面编辑器主窗口（独立 Toplevel）。"""

    LANE_W = 132
    JUDGE_H = 44          # 判定线离 note 区底部的高度

    def __init__(self, parent, chart_path="", log=print):
        self.parent = parent
        self.doc = None
        self.log = log
        self.bind_map = dict(DEFAULT_BINDS)
        self.zoom = 1.0
        self.density = 16
        self.view_time = 0.0          # 判定线对应的歌曲时间
        self.playing = False
        self.play_start_wall = 0.0
        self.play_start_song = 0.0
        self.selected = None          # 选中的 note
        self._last_frame = 0.0
        self.audio = fosu_audio.AudioEngine()   # 真实音频播放（失败自动静音时钟兜底）
        self.audio_loaded = False
        self.play_head_ms = 0.0        # 音频播放头（未播放时保持的位置）
        self._audio_started = False    # 当前播放是否由真实音频驱动
        self._play_t0 = 0.0            # 起播时刻（宽限期内不查 mode，防异步误判 stopped）
        self._seek_pending = False     # 滚轮/点击 seek 防抖中
        self._seek_timer = None
        self.vol_var = tk.IntVar(value=80)

        self.win = tk.Toplevel(parent)
        self.win.title("FNF 谱面编辑器（Psych Engine 格式 · 全汉化）")
        self.win.geometry("1080x820")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        if chart_path:
            self.open_file(chart_path)

    # ------------------------------------------------------------ 界面
    def _build(self):
        w = self.win
        bar = ttk.Frame(w, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="📂 打开谱面", command=self.pick_open).pack(side="left")
        ttk.Button(bar, text="💾 保存", command=self.pick_save).pack(side="left", padx=4)
        ttk.Button(bar, text="💾 另存为", command=self.pick_save_as).pack(side="left")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.play_btn = ttk.Button(bar, text="▶ 播放（边播边输入）",
                                   command=self.toggle_play)
        self.play_btn.pack(side="left")
        ttk.Button(bar, text="⏮ 回到开头", command=self.go_start).pack(side="left", padx=4)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(bar, text="量化网格：").pack(side="left")
        self.dens_var = tk.StringVar(value="1/16")
        cb = ttk.Combobox(bar, textvariable=self.dens_var, width=6, state="readonly",
                          values=["1/4", "1/8", "1/16", "1/32"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._set_density())
        ttk.Label(bar, text="  缩放：").pack(side="left")
        ttk.Button(bar, text="－", width=3, command=lambda: self.set_zoom(self.zoom / 1.25)).pack(side="left")
        self.zoom_lbl = ttk.Label(bar, text="100%", width=5)
        self.zoom_lbl.pack(side="left")
        ttk.Button(bar, text="＋", width=3, command=lambda: self.set_zoom(self.zoom * 1.25)).pack(side="left")
        ttk.Button(bar, text="⌨ 键位设置", command=self.open_keybinds).pack(side="left", padx=8)

        # 音频栏：导入音频 → 真实播放 + 波形
        abar = ttk.Frame(w, padding=(8, 2))
        abar.pack(fill="x")
        ttk.Button(abar, text="🎵 导入音频", command=self.pick_audio).pack(side="left")
        ttk.Button(abar, text="🔄 自动找同目录音频",
                   command=self.auto_audio).pack(side="left", padx=4)
        self.audio_lbl = ttk.Label(abar, text="未载入音频（可用无声音频时钟练习）",
                                   foreground="#888")
        self.audio_lbl.pack(side="left", padx=8)
        ttk.Label(abar, text="音量").pack(side="left", padx=(12, 2))
        ttk.Scale(abar, from_=0, to=100, variable=self.vol_var,
                  command=self.on_volume, length=110).pack(side="left")
        ttk.Label(abar, text="（点击时间轴可跳转）", foreground="#888").pack(side="left", padx=8)

        body = ttk.Frame(w)
        body.pack(fill="both", expand=True, padx=8, pady=4)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, minsize=320)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(0, weight=3)
        left.rowconfigure(1, weight=1)
        self.cv_notes = tk.Canvas(left, background="#14161c", highlightthickness=0)
        self.cv_notes.grid(row=0, column=0, sticky="nsew")
        self.cv_grid = tk.Canvas(left, background="#0f1116", highlightthickness=0)
        self.cv_grid.grid(row=1, column=0, sticky="nsew")

        # 右侧事件面板
        right = ttk.LabelFrame(body, text=" 特殊事件（相机 / 角色 / 声音） ", padding=6)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.ev_tree = ttk.Treeview(right, columns=("t", "type", "v1", "v2"),
                                     show="headings", height=18)
        for c, t, wd in (("t", "时间(ms)", 90), ("type", "类型", 120),
                         ("v1", "参数1", 80), ("v2", "参数2", 80)):
            self.ev_tree.heading(c, text=t)
            self.ev_tree.column(c, width=wd, anchor="w")
        self.ev_tree.pack(fill="both", expand=True)
        self.ev_tree.bind("<Delete>", lambda e: self.del_event())

        eb = ttk.Frame(right)
        eb.pack(fill="x", pady=(6, 0))
        self.ev_type = tk.StringVar(value=EVENT_TYPES_PE[0])
        self.ev_v1 = tk.StringVar(value="")
        self.ev_v2 = tk.StringVar(value="")
        ttk.Label(eb, text="类型").grid(row=0, column=0, sticky="w")
        ttk.Combobox(eb, textvariable=self.ev_type, width=16,
                     values=EVENT_TYPES_PE + EVENT_TYPES_FREE).grid(row=0, column=1, sticky="ew")
        ttk.Label(eb, text="参数1").grid(row=1, column=0, sticky="w")
        ttk.Entry(eb, textvariable=self.ev_v1, width=18).grid(row=1, column=1, sticky="ew")
        ttk.Label(eb, text="参数2").grid(row=2, column=0, sticky="w")
        ttk.Entry(eb, textvariable=self.ev_v2, width=18).grid(row=2, column=1, sticky="ew")
        eb.columnconfigure(1, weight=1)
        ttk.Button(eb, text="➕ 在当前时间添加", command=self.add_event).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(eb, text="🗑 删除选中事件", command=self.del_event).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        self.status = tk.StringVar(value="未载入谱面")
        ttk.Label(w, textvariable=self.status, relief="sunken", anchor="w").pack(
            fill="x", side="bottom")

        # 交互绑定
        self.cv_notes.bind("<Button-1>", self.on_click_notes)
        self.cv_notes.bind("<MouseWheel>", self.on_wheel)
        self.cv_grid.bind("<MouseWheel>", self.on_wheel)
        self.cv_grid.bind("<Button-1>", self.on_click_grid)   # 点击时间轴跳转
        self.cv_notes.bind("<Configure>", lambda e: self.redraw())
        self.cv_grid.bind("<Configure>", lambda e: self.redraw())
        self.win.bind("<Key>", self.on_key)
        self.win.focus_set()

    # ------------------------------------------------------------ 文件
    def pick_open(self):
        p = filedialog.askopenfilename(
            title="打开 FNF 谱面 JSON", parent=self.win,
            filetypes=[("FNF 谱面", "*.json"), ("所有文件", "*.*")])
        if p:
            self.open_file(p)

    def open_file(self, path):
        try:
            self.doc = ChartDoc.load(path)
        except Exception as e:
            messagebox.showerror("打开失败", str(e), parent=self.win)
            return
        self.view_time = 0.0
        self.selected = None
        self.doc.invalidate()
        self.refresh_events()
        self.redraw()
        self.status.set(f"已载入：{os.path.basename(path)}　"
                        f"{self.doc.title}　{self.doc.keys}K/侧　"
                        f"BPM {self.doc.bpm:g}　事件 {len(self.doc.event_rows())} 条")
        # 打开谱面自动探测同目录 Inst/Voices（不阻塞界面）
        found = fosu_audio.guess_audio_for_chart(path)
        if found:
            self.win.after(150, lambda: self.load_audio_files(found, quiet=True))

    def pick_save(self):
        if not self.doc:
            messagebox.showinfo("提示", "还没打开谱面", parent=self.win)
            return
        try:
            p = self.doc.save()
            self.status.set("已保存：" + p)
            self.log("谱面已保存 -> " + p)
        except Exception as e:
            messagebox.showerror("保存失败", str(e), parent=self.win)

    def pick_save_as(self):
        if not self.doc:
            messagebox.showinfo("提示", "还没打开谱面", parent=self.win)
            return
        p = filedialog.asksaveasfilename(
            title="另存为", parent=self.win, defaultextension=".json",
            initialfile=os.path.basename(self.doc.path or "chart.json"),
            filetypes=[("FNF 谱面 JSON", "*.json")])
        if p:
            try:
                self.doc.save(p)
                self.status.set("已保存：" + p)
            except Exception as e:
                messagebox.showerror("保存失败", str(e), parent=self.win)

    # ------------------------------------------------------------ 音频
    def pick_audio(self):
        """手动导入音频（可多选，自动 Inst+Voices 混音）。"""
        paths = filedialog.askopenfilenames(
            title="选择音频（可多选，Inst + Voices 会自动混音）", parent=self.win,
            filetypes=[("音频文件", "*.ogg *.mp3 *.wav *.flac *.m4a *.mp4 *.aac *.wma *.opus"),
                       ("所有文件", "*.*")])
        if paths:
            self.load_audio_files(list(paths))

    def auto_audio(self):
        """重新扫描谱面同目录的音频。"""
        p = self.doc.path if self.doc else ""
        found = fosu_audio.guess_audio_for_chart(p)
        if not found:
            self.audio_lbl.configure(text="同目录没找到音频文件", foreground="#c96")
            return
        self.load_audio_files(found)

    def load_audio_files(self, paths, quiet=False):
        """载入音频：ffmpeg 转码 + MCI 打开；失败则静音时钟兜底。"""
        if self.playing:
            self._stop_playback()
        self.audio_loaded = self.audio.load(paths, log=self.log)
        if self.audio_loaded:
            names = " + ".join(os.path.basename(p) for p in self.audio.source_paths)
            self.audio_lbl.configure(
                text=f"♪ {names}　{self.audio.duration_ms/1000:.1f}s　波形 {len(self.audio.peaks)} 点",
                foreground="#4c9")
            self.audio.set_volume(self.vol_var.get())
            if not quiet:
                self.log("音频已导入并可播放（空格键播放/停止）")
        else:
            self.audio_lbl.configure(text="音频载入失败，改用静音播放头", foreground="#c96")
        self.redraw()

    def on_volume(self, _v=None):
        if self.audio_loaded:
            self.audio.set_volume(self.vol_var.get())

    def on_click_grid(self, event):
        """点击时间轴 → 跳到该时间（并同步音频播放头）。"""
        if not self.doc:
            return
        ppm = self.px_per_ms()
        if ppm <= 0:
            return
        # 时间轴 x=0 即当前 view_time，向右为未来
        target = self.view_time + (event.x / ppm)
        self.seek(max(0.0, target))

    def seek(self, t):
        """把播放头/视图时间设到 t；播放中则防抖同步音频位置。

        滚轮会连发几十次事件，逐次 play_from 会不断重启音频（卡顿/爆音），
        所以合并成 180ms 内最后一次才真正 seek。
        """
        self.view_time = max(0.0, t)
        self.play_head_ms = self.view_time
        if self.playing and self.audio_loaded and self._audio_started:
            self._seek_pending = True
            if self._seek_timer:
                try:
                    self.win.after_cancel(self._seek_timer)
                except Exception:
                    pass
            self._seek_timer = self.win.after(180, self._apply_seek)
        self.redraw()

    def _apply_seek(self):
        self._seek_timer = None
        self._seek_pending = False
        if not self.playing or not self.audio_loaded:
            return
        self.audio.play_from(self.view_time)
        self._play_t0 = time.time()
        self.play_start_wall = time.time()
        self.play_start_song = self.view_time
        self._audio_started = True

    # ------------------------------------------------------------ 时间/缩放
    def _set_density(self):
        self.density = int(self.dens_var.get().split("/")[1])
        self.redraw()

    def set_zoom(self, z):
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, z))
        self.zoom_lbl.configure(text=f"{int(self.zoom * 100)}%")
        self.redraw()

    def px_per_ms(self):
        base = 0.45 * (self.doc.speed if self.doc else 2.0)
        return base * self.zoom

    def go_start(self):
        self.seek(0.0)

    # ------------------------------------------------------------ 播放
    def toggle_play(self):
        if self.playing:
            self._stop_playback()
            return
        self.playing = True
        self.play_btn.configure(text="⏸ 停止")
        self.play_start_wall = time.time()
        self.play_start_song = self.view_time
        self._play_t0 = time.time()
        self._audio_started = False
        if self.audio_loaded:
            # 从当前判定线位置起播，音频与画面用同一个 position 驱动
            self._audio_started = self.audio.play_from(self.view_time)
            if not self._audio_started:
                self.audio_lbl.configure(text="音频起播失败，改用静音播放头", foreground="#c96")
        self._tick()

    def _stop_playback(self):
        self.playing = False
        self._seek_pending = False
        if self._seek_timer:
            try:
                self.win.after_cancel(self._seek_timer)
            except Exception:
                pass
            self._seek_timer = None
        self.play_btn.configure(text="▶ 播放（边播边输入）")
        if self.audio_loaded:
            pos = self.audio.position()
            if pos is not None:
                self.view_time = pos
            self.audio.stop()
        self._audio_started = False
        self.redraw()

    def _tick(self):
        if not self.playing:
            return
        # seek 防抖期间：保持用户拖到的视图，别被音频 position 拉回
        if self._seek_pending:
            self.redraw()
            self.win.after(16, self._tick)
            return
        if self._audio_started and self.audio_loaded:
            pos = self.audio.position()
            if pos is None:
                # 读不到位置 → 退化成静音时钟，别卡死
                self._audio_started = False
                self.play_start_wall = time.time()
                self.play_start_song = self.view_time
            else:
                self.view_time = pos          # 播放头 = 真实音频位置，零漂移
                # 播到结尾自动停
                if self.audio.duration_ms > 0 and pos >= self.audio.duration_ms - 25:
                    self._stop_playback()
                    return
                # 刚起播的 0.5s 内不查 mode（MCI 异步启动，可能短暂报 stopped）
                if time.time() - self._play_t0 > 0.5 and not self.audio.is_playing():
                    self._stop_playback()
                    return
        else:
            now = time.time()
            self.view_time = self.play_start_song + (now - self.play_start_wall) * 1000.0
        self.redraw()
        self.win.after(16, self._tick)

    # ------------------------------------------------------------ 输入
    def on_key(self, event):
        if not self.doc:
            return
        ch = event.keysym.lower()
        if ch == "space":
            self.toggle_play()
            return
        if ch in ("delete", "backspace"):
            self.delete_selected()
            return
        if ch == "left":
            self.seek(self.view_time - self.beat_len() / 2)
            return
        if ch == "right":
            self.seek(self.view_time + self.beat_len() / 2)
            return
        if ch in ("plus", "equal"):
            self.set_zoom(self.zoom * 1.25)
            return
        if ch == "minus":
            self.set_zoom(self.zoom / 1.25)
            return
        lane = self.bind_map.get(ch)
        if lane is None:
            return
        # 键盘输入：落在判定线时刻（自动网格校正）
        t = self.doc.quantize(self.view_time, self.density)
        self.doc.add_note(t, lane, 0.0, must_hit=True)
        self.redraw()
        self.status.set(f"已输入 {DIR_CN[lane]} @ {t:.0f}ms（量化 1/{self.density}）")

    def beat_len(self):
        return self.doc.beat_ms_at(self.view_time) if self.doc else 500.0

    def on_wheel(self, event):
        delta = event.delta
        if event.state & 0x4:      # Ctrl -> 缩放
            self.set_zoom(self.zoom * (1.25 if delta > 0 else 0.8))
            return
        step = self.beat_len()
        self.seek(self.view_time - delta / 120.0 * step)

    def on_click_notes(self, event):
        if not self.doc:
            return
        h = self.cv_notes.winfo_height()
        judge_y = h - self.JUDGE_H
        lane = self.lane_at_x(event.x)
        if lane is None:
            return
        t = self.doc.quantize(self.view_time, self.density)
        if event.y < judge_y:
            # note 区域：选中（高亮），删除请按 Delete
            hit = self.find_note(t, lane)
            self.selected = hit
            self.redraw()
            if hit:
                self.status.set(f"已选中 {DIR_CN[lane]} @ {hit['time']:.0f}ms（按 Delete 删除）")
            return
        # 判定线附近点击 = 放 note
        self.doc.add_note(t, lane, 0.0, must_hit=True)
        self.redraw()
        self.status.set(f"已输入 {DIR_CN[lane]} @ {t:.0f}ms")

    def lane_at_x(self, x):
        n = self.doc.keys if self.doc else 4
        idx = int(x // self.LANE_W)
        return idx if 0 <= idx < n else None

    def find_note(self, t, lane, tol=12.0):
        for nt in self.doc.all_notes():
            if abs(nt["time"] - t) <= tol and lane in nt["lanes"]:
                return nt
        return None

    def delete_selected(self):
        if self.selected:
            self.doc.remove_note(self.selected)
            self.selected = None
            self.redraw()

    # ------------------------------------------------------------ 事件
    def refresh_events(self):
        for i in self.ev_tree.get_children():
            self.ev_tree.delete(i)
        if not self.doc:
            return
        for t, ty, v1, v2, _ in self.doc.event_rows():
            self.ev_tree.insert("", "end", values=(f"{t:.0f}", ty, v1, v2))

    def add_event(self):
        if not self.doc:
            return
        self.doc.add_event(self.view_time, self.ev_type.get(),
                           self.ev_v1.get(), self.ev_v2.get())
        self.refresh_events()
        self.redraw()
        self.status.set(f"已添加事件：{self.ev_type.get()} @ {self.view_time:.0f}ms")

    def del_event(self):
        if not self.doc:
            return
        sel = self.ev_tree.selection()
        if not sel:
            return
        iid = sel[0]
        vals = self.ev_tree.item(iid, "values")
        rows = self.doc.event_rows()
        target = None
        for r in rows:
            if f"{r[0]:.0f}" == str(vals[0]) and r[1] == vals[1]:
                target = r
                break
        if target:
            self.doc.remove_event(target)
            self.refresh_events()
            self.redraw()

    # ------------------------------------------------------------ 键位
    def open_keybinds(self):
        d = tk.Toplevel(self.win)
        d.title("键位设置")
        d.transient(self.win)
        d.resizable(False, False)
        ttk.Label(d, text="点「重新绑定」后按下想用的键（Esc 取消该方向）",
                  padding=8).grid(row=0, column=0, columnspan=3)
        labels = ["左", "下", "上", "右"]
        cur = {lane: next((k for k, v in self.bind_map.items() if v == lane), "?")
               for lane in range(4)}
        vars_ = {lane: tk.StringVar(value=cur[lane].upper()) for lane in range(4)}
        waiting = {"lane": None}

        def rebind(lane):
            waiting["lane"] = lane
            vars_[lane].set("按下按键…")
            d.focus_set()

        for lane in range(4):
            ttk.Label(d, text=f"{labels[lane]}：").grid(row=1 + lane, column=0,
                                                    sticky="w", padx=8)
            ttk.Label(d, textvariable=vars_[lane], width=12).grid(
                row=1 + lane, column=1, sticky="e", padx=4)
            ttk.Button(d, text="重新绑定", command=lambda l=lane: rebind(l)).grid(
                row=1 + lane, column=2, padx=8)

        def on_press(e):
            lane = waiting["lane"]
            if lane is None:
                return
            ch = e.keysym.lower()
            if ch == "escape":
                vars_[lane].set("?")
            else:
                vars_[lane].set(ch.upper())
            waiting["lane"] = None

        def ok():
            newmap = {}
            for lane in range(4):
                k = vars_[lane].get().lower()
                if k and k != "?":
                    newmap[k] = lane
            if not newmap:
                messagebox.showwarning("键位", "至少要设置一个按键", parent=d)
                return
            self.bind_map = newmap
            d.destroy()
            self.status.set("键位已更新：" +
                            "、".join(f"{DIR_CN[v]}={k.upper()}" for k, v in self.bind_map.items()))
            self.redraw()

        d.bind("<Key>", on_press)
        ttk.Button(d, text="确定", command=ok).grid(row=5, column=0, columnspan=2,
                                                padx=8, pady=8)
        ttk.Button(d, text="取消", command=d.destroy).grid(row=5, column=2, padx=8, pady=8)
        d.focus_set()

    # ------------------------------------------------------------ 绘制
    def redraw(self):
        self.draw_notes()
        self.draw_grid()

    def draw_notes(self):
        cv = self.cv_notes
        cv.delete("all")
        if not self.doc:
            cv.create_text(20, 20, text="打开一份 FNF 谱面开始编辑（支持拖入 JSON）",
                           fill="#667", anchor="nw")
            return
        w = cv.winfo_width() or 900
        h = cv.winfo_height() or 400
        if w <= 1:
            return
        keys = self.doc.keys
        ppm = self.px_per_ms()
        judge_y = h - self.JUDGE_H

        # 轨道背景
        for i in range(keys):
            x0 = i * self.LANE_W
            cv.create_rectangle(x0, 0, x0 + self.LANE_W, h,
                                fill="#1b1f2a" if i % 2 == 0 else "#171b24",
                                outline="#2a3040")
            cv.create_text(x0 + self.LANE_W / 2, 12, text=DIR_CN[i % 4],
                           fill="#8a93a8")

        # note（只画可视时间窗口，避免上千个 note 每帧全画卡死）
        notes = self.doc.all_notes()
        times = [n["time"] for n in notes]
        i = bisect.bisect_left(times, self.view_time - 300)
        j = bisect.bisect_right(times, self.view_time + 4000)
        for nt in notes[i:j]:
            dt = nt["time"] - self.view_time
            y = judge_y - dt * ppm
            for lane in nt["lanes"]:
                if lane < 0 or lane >= keys:
                    continue
                x0 = lane * self.LANE_W
                color = "#4ea1ff" if nt["must_hit"] else "#ff7a5c"
                if nt["length"] > 0:
                    y2 = y - nt["length"] * ppm
                    cv.create_rectangle(x0 + 22, min(y, y2), x0 + self.LANE_W - 22,
                                        max(y, y2), fill=color, outline="")
                else:
                    cv.create_oval(x0 + 30, y - 13, x0 + self.LANE_W - 30, y + 13,
                                   fill=color, outline="#0b0d12")
        # 判定线
        cv.create_line(0, judge_y, keys * self.LANE_W, judge_y, fill="#f2f5ff", width=2)
        cv.create_text(4, judge_y - 12, text=f"判定线 {self.view_time:.0f}ms",
                       fill="#f2f5ff", anchor="w")

    def _draw_waveform(self, cv, w, h, ppm):
        """在时间轴上叠一层音频波形，方便对齐鼓点。"""
        if not self.audio_loaded or not self.audio.peaks or ppm <= 0:
            return
        peaks = self.audio.peaks
        pms = self.audio.peak_ms
        mid = h * 0.5
        amp = min(h * 0.40, 70)
        i0 = max(0, int(self.view_time / pms) - 2)
        i1 = min(len(peaks), int((self.view_time + w / ppm) / pms) + 2)
        for i in range(i0, i1):
            x = (i * pms - self.view_time) * ppm
            if x < -2 or x > w + 2:
                continue
            a = peaks[i] * amp
            if a < 0.6:
                a = 0.6
            cv.create_line(x, mid - a, x, mid + a, fill="#2f6f78")

    def draw_grid(self):
        cv = self.cv_grid
        cv.delete("all")
        w = cv.winfo_width() or 900
        h = cv.winfo_height() or 200
        if w <= 1:
            return
        ppm = self.px_per_ms()
        # 波形先画（作背景），网格线叠在上层
        self._draw_waveform(cv, w, h, ppm)
        if not self.doc:
            # 没打开谱面也能听音频：至少画个播放头
            cv.create_line(0, 0, 0, h, fill="#ffe14d", width=2)
            cv.create_text(4, h - 14, text=f"{self.view_time:.0f}ms",
                           fill="#ffe14d", anchor="w")
            return
        beat = self.beat_len()
        # 网格：每 1/density 拍一条细线，每拍中线，每小节粗线
        step = beat / self.density
        start = self.view_time - beat
        end = self.view_time + (w / ppm if ppm else 2000)
        t = start - (start % step)
        small = beat / 4.0
        i = 0
        while t <= end:
            x = (t - self.view_time) * ppm
            is_beat = abs((t / beat) - round(t / beat)) < 1e-3
            is_bar = abs((t / (beat * 4)) - round(t / (beat * 4))) < 1e-3
            if is_bar:
                cv.create_line(x, 0, x, h, fill="#39415c", width=2)
                cv.create_text(x + 3, 10, text=f"{t/1000:.2f}s", fill="#7f8aa5", anchor="nw")
            elif is_beat:
                cv.create_line(x, 0, x, h, fill="#2b3244")
            else:
                cv.create_line(x, 0, x, h, fill="#1d2230")
            t += step
            i += 1
            if i > 4000:
                break
        # 播放头
        x = 0
        cv.create_line(x, 0, x, h, fill="#ffe14d", width=2)
        cv.create_text(4, h - 14, text=f"{self.view_time:.0f}ms", fill="#ffe14d", anchor="w")

    def load_external_audio(self, path):
        """供主窗口拖拽导入调用：把音频文件喂给编辑器。"""
        self.load_audio_files([path])

    def close(self):
        self.playing = False
        self._seek_pending = False
        if self._seek_timer:
            try:
                self.win.after_cancel(self._seek_timer)
            except Exception:
                pass
        try:
            self.audio.close()
        except Exception:
            pass
        self.win.destroy()


def open_editor(parent, chart_path="", log=print):
    return EditorWindow(parent, chart_path, log)
