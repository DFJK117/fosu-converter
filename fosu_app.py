# -*- coding: utf-8 -*-
"""
fosu - FNF (PsychEngine) -> osu!mania 一键转谱器
GUI 主程序（tkinter，标准库，无第三方依赖）。
用法:
  双击 fosu.exe                 打开图形界面
  fosu.exe --selftest 谱面.json Inst.ogg Voices.ogg 输出.osz   命令行自测
"""
import os
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fosu_core as fc

APP_NAME = "fosu"
APP_VER = "1.0"

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
    def __init__(self, root: tk.Tk):
        self.root = root
        self.chart_var = tk.StringVar()
        self.inst_var = tk.StringVar()
        self.voices_var = tk.StringVar()
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
        self.root.title(f"{APP_NAME} {APP_VER} — FNF → osu!mania 一键转谱器")
        self.root.geometry("820x760")
        self.root.minsize(760, 700)
        pad = {"padx": 8, "pady": 3}

        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        # ① 谱面
        self._section(frm, 0, "① 选择 PsychEngine 谱面 JSON")
        ttk.Label(frm, text="谱面：").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.chart_var).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="浏览…", command=self.pick_chart).grid(row=1, column=2, **pad)
        ttk.Label(frm, textvariable=self.info_var, foreground="#0a6",
                  justify="left", wraplength=720).grid(row=2, column=0, columnspan=3,
                                                       sticky="w", padx=8)

        # ② 音频
        self._section(frm, 3, "② 音频（选中谱面后自动识别同目录文件）")
        ttk.Label(frm, text="Inst：").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.inst_var).grid(row=4, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="浏览…", command=lambda: self.pick_audio(self.inst_var)).grid(
            row=4, column=2, **pad)
        ttk.Label(frm, text="Voices：").grid(row=5, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.voices_var).grid(row=5, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="浏览…", command=lambda: self.pick_audio(self.voices_var)).grid(
            row=5, column=2, **pad)
        row6 = ttk.Frame(frm)
        row6.grid(row=6, column=0, columnspan=3, sticky="w", padx=8)
        ttk.Checkbutton(row6, text="混音 Inst + Voices", variable=self.mix_var).pack(side="left")
        ttk.Label(row6, text="  Inst 音量").pack(side="left")
        ttk.Spinbox(row6, from_=0, to=2, increment=0.05, width=5,
                    textvariable=self.inst_vol).pack(side="left")
        ttk.Label(row6, text="  Voices 音量").pack(side="left")
        ttk.Spinbox(row6, from_=0, to=2, increment=0.05, width=5,
                    textvariable=self.voices_vol).pack(side="left")

        # ③ 谱面选项
        self._section(frm, 7, "③ 谱面选项")
        row8 = ttk.Frame(frm)
        row8.grid(row=8, column=0, columnspan=3, sticky="w", padx=8)
        ttk.Label(row8, text="每侧键数：").pack(side="left")
        ttk.Combobox(row8, textvariable=self.keys_var, width=8, state="readonly",
                     values=["自动", "4", "5", "6", "7", "8", "9"]).pack(side="left")
        ttk.Label(row8, text="  导出音符：").pack(side="left")
        ttk.Combobox(row8, textvariable=self.side_var, width=12, state="readonly",
                     values=["BF（玩家）", "对手", "全部"]).pack(side="left")
        ttk.Checkbutton(row8, text="过滤扣血音符（noteType 3）",
                        variable=self.damage_var).pack(side="left", padx=12)

        row9 = ttk.Frame(frm)
        row9.grid(row=9, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        ttk.Label(row9, text="难度名：").pack(side="left")
        ttk.Entry(row9, textvariable=self.version_var, width=12).pack(side="left")
        ttk.Label(row9, text="  艺术家：").pack(side="left")
        ttk.Entry(row9, textvariable=self.artist_var, width=18).pack(side="left")

        row10 = ttk.Frame(frm)
        row10.grid(row=10, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        ttk.Label(row10, text="HP：").pack(side="left")
        ttk.Spinbox(row10, from_=1, to=10, increment=0.5, width=5,
                    textvariable=self.hp_var).pack(side="left")
        ttk.Label(row10, text="  OD：").pack(side="left")
        ttk.Spinbox(row10, from_=1, to=10, increment=0.5, width=5,
                    textvariable=self.od_var).pack(side="left")
        ttk.Label(row10, text="  时间偏移(ms)：").pack(side="left")
        ttk.Spinbox(row10, from_=-2000, to=2000, increment=10, width=7,
                    textvariable=self.offset_var).pack(side="left")
        ttk.Label(row10, text="  mania 滚动倍率：").pack(side="left")
        ttk.Spinbox(row10, from_=0.5, to=4, increment=0.1, width=5,
                    textvariable=self.sm_var).pack(side="left")

        # ④ 输出
        self._section(frm, 11, "④ 输出 .osz（osu! 谱面包，双击即可导入 osu!）")
        ttk.Label(frm, text="输出：").grid(row=12, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.out_var).grid(row=12, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="另存为…", command=self.pick_out).grid(row=12, column=2, **pad)

        self.go_btn = ttk.Button(frm, text="开始转换", command=self.on_go)
        self.go_btn.grid(row=13, column=0, columnspan=3, pady=8)

        self.logbox = tk.Text(frm, height=12, wrap="word", state="disabled",
                              background="#111", foreground="#ddd", font=("Consolas", 9))
        self.logbox.grid(row=14, column=0, columnspan=3, sticky="nsew", padx=8, pady=4)
        frm.rowconfigure(14, weight=1)

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
            song = fc.load_chart(p)
            layout = fc.detect_layout(song)
            keys = layout["keys"]
            st = fc.chart_stats(song, keys)
            bf = fc.collect_notes(song, side="bf", keys=keys)
            holds = sum(1 for n in bf if n["length"] > 0)
            bpm = song.get("bpm", "?")
            title = fc.prettify(str(song.get("song", os.path.basename(p))))
            mf = layout["mania_field"]
            mf_txt = f"有（={mf}，与实际不符时已自动按实际轨道）" if mf is not None else "无（普通 4K 谱）"
            self.info_var.set(
                f"曲名：{title}　BPM：{bpm}　mania 字段：{mf_txt}\n"
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
            if not self.out_var.get():
                name = fc.safe_name(
                    f"{self.artist_var.get()} - {title} (fosu)[{self.version_var.get()}].osz")
                self.out_var.set(os.path.join(d, name))
            self.log(f"已载入谱面：{p}")
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
        if inst and not os.path.isfile(inst):
            messagebox.showwarning("音频缺失", f"找不到 Inst 音频：\n{inst}")
            return
        if voices and not os.path.isfile(voices):
            messagebox.showwarning("音频缺失", f"找不到 Voices 音频：\n{voices}")
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
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    FosuGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
