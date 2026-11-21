# -*- coding: utf-8 -*-
"""
fosu_audio.py —— 内置编谱器的音频引擎（纯标准库，无第三方依赖）

方案：ffmpeg 把任意音频（ogg/mp3/wav/flac/mp4…，支持 Inst+Voices 混音）
      解码成临时 PCM WAV → WinMM MCI 播放。
MCI 提供毫秒级 position / seek / 音量，播放头直接跟着音频走（零漂移），
且完全不依赖窗口焦点（不像 SendInput 那套前台锁）。

所有函数失败时都优雅降级返回 False/None，编辑器会自动退回"静音时钟"模式。
"""
import array
import os
import subprocess
import sys
import tempfile
import wave
from ctypes import windll, create_unicode_buffer, c_ulong, WINFUNCTYPE
import ctypes

CREATE_NO_WINDOW = 0x08000000
AUDIO_EXTS = (".ogg", ".mp3", ".wav", ".flac", ".m4a", ".mp4", ".aac", ".wma", ".opus")


def find_ffmpeg():
    """定位内置的 ffmpeg.exe（冻结态在 _MEIPASS / exe 同目录）。"""
    cands = []
    if getattr(sys, "frozen", False):
        cands += [os.path.join(getattr(sys, "_MEIPASS", ""), "ffmpeg.exe"),
                  os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe")]
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return "ffmpeg"


class MCI:
    """WinMM mciSendStringW 极简封装。"""

    def __init__(self):
        self.ok = False
        self.alias = "fosu_song"
        if os.name == "nt":
            try:
                self.winmm = windll.winmm
                self.mci = self.winmm.mciSendStringW
                self.mci.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                                     ctypes.c_uint, ctypes.c_void_p]
                self.mci.restype = ctypes.c_uint
                self.ok = True
            except Exception:
                self.ok = False

    def cmd(self, s, fetch=False):
        if not self.ok:
            return (1, "")
        buf = create_unicode_buffer(256)
        # fetch 时用回调拿返回值：mciSendStringW 第 2 参数是返回缓冲
        r = self.mci(s, buf, 256, None)
        return (r, buf.value)

    def send(self, s):
        return self.cmd(s)[0] == 0


class AudioEngine:
    """管理：转码 → MCI 播放 → 播放头位置 → 波形峰值。"""

    def __init__(self):
        self.mci = MCI()
        self.wav_path = ""
        self.source_paths = []
        self.peaks = []          # 0..1，每 PEAK_MS 一个
        self.peak_ms = 20.0
        self.duration_ms = 0.0
        self.loaded = False
        self.volume = 80         # 0..100
        self._start_ms = 0.0

    # ------------------------------------------------------------ 载入
    def load(self, paths, log=print):
        """paths: 单个文件或 [inst, voices]（自动混音）。返回 True/False。"""
        if isinstance(paths, str):
            paths = [paths]
        paths = [p for p in paths if p and os.path.isfile(p)]
        if not paths:
            return False
        self.close()
        fd, wav = tempfile.mkstemp(prefix="fosu_audio_", suffix=".wav")
        os.close(fd)
        try:
            self._transcode(paths, wav)
            self._build_peaks(wav)
        except Exception as e:
            log("音频解码失败：%s" % e)
            try:
                os.remove(wav)
            except Exception:
                pass
            return False
        self.wav_path = wav
        self.source_paths = paths
        # MCI 打开：type waveaudio 显式指定，避免按扩展名猜
        q = wav.replace("\\", "/")
        if not self.mci.send('open "%s" type waveaudio alias %s' % (q, self.mci.alias)):
            # 退路：不带 type
            if not self.mci.send('open "%s" alias %s' % (q, self.mci.alias)):
                log("MCI 打开音频失败（仍可用静音播放头）")
                return False
        self.mci.send("set %s time format ms" % self.mci.alias)
        r, length = self.mci.cmd("status %s length" % self.mci.alias, fetch=True)
        try:
            self.duration_ms = float(length)
        except Exception:
            self.duration_ms = len(self.peaks) * self.peak_ms
        self.set_volume(self.volume)
        self.loaded = True
        names = " + ".join(os.path.basename(p) for p in paths)
        log("已载入音频：%s（%.1f 秒）" % (names, self.duration_ms / 1000.0))
        return True

    def _transcode(self, paths, out_wav):
        ff = find_ffmpeg()
        kw = dict(creationflags=CREATE_NO_WINDOW) if os.name == "nt" else {}
        common = [ff, "-y", "-loglevel", "error", "-nostdin"]
        if len(paths) >= 2:
            # 两轨混音（Inst + Voices），duration=longest 保证不截断
            args = common + ["-i", paths[0], "-i", paths[1],
                             "-filter_complex",
                             "[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0",
                             "-ar", "22050", "-ac", "1", "-c:a", "pcm_s16le", out_wav]
        else:
            args = common + ["-i", paths[0],
                             "-ar", "22050", "-ac", "1", "-c:a", "pcm_s16le", out_wav]
        p = subprocess.run(args, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, **kw)
        if p.returncode != 0 or not os.path.isfile(out_wav) or os.path.getsize(out_wav) < 44:
            msg = p.stderr.decode("utf-8", "ignore")[-400:] if p.stderr else "无输出"
            raise RuntimeError(msg)

    def _build_peaks(self, wav_path, peak_ms=20.0):
        """从 WAV 采样出波形峰值（每 peak_ms 一个，0..1），供时间轴画波形。"""
        with wave.open(wav_path, "rb") as wf:
            rate = wf.getframerate()
            nframes = wf.getnframes()
            raw = wf.readframes(nframes)
        samples = array.array("h")
        samples.frombytes(raw)
        step = max(1, int(rate * peak_ms / 1000.0))
        peaks = []
        n = len(samples)
        for i in range(0, n, step):
            chunk = samples[i:i + step]
            if not chunk:
                break
            mx = max(chunk)
            mn = min(chunk)
            v = max(abs(mx), abs(mn)) / 32768.0
            peaks.append(min(1.0, v))
        self.peaks = peaks
        self.peak_ms = peak_ms
        if not self.duration_ms:
            self.duration_ms = len(peaks) * peak_ms

    # ------------------------------------------------------------ 播放
    def play_from(self, ms):
        if not self.loaded or not self.mci.ok:
            return False
        ms = max(0.0, ms)
        if ms >= self.duration_ms - 30:
            ms = 0.0
        self._start_ms = ms
        # 先 seek 再 play，避免部分实现不支持 from
        self.mci.send("seek %s to %d" % (self.mci.alias, int(ms)))
        ok = self.mci.send("play %s from %d" % (self.mci.alias, int(ms)))
        return ok

    def pause(self):
        if self.loaded:
            return self.mci.send("pause %s" % self.mci.alias)
        return False

    def resume(self):
        if self.loaded:
            return self.mci.send("resume %s" % self.mci.alias)
        return False

    def stop(self):
        if self.loaded:
            return self.mci.send("stop %s" % self.mci.alias)
        return False

    def position(self):
        """当前播放位置(ms)；未载入/失败返回 None。"""
        if not self.loaded or not self.mci.ok:
            return None
        r, s = self.mci.cmd("status %s position" % self.mci.alias, fetch=True)
        try:
            return float(s)
        except Exception:
            return None

    def mode(self):
        r, s = self.mci.cmd("status %s mode" % self.mci.alias, fetch=True)
        return s.strip().lower()

    def is_playing(self):
        return self.mode() == "playing"

    def set_volume(self, v):
        """0..100。waveaudio 不支持 setaudio，退回 waveOutSetVolume（实时、局部设备）。"""
        self.volume = max(0, min(100, int(v)))
        if not self.loaded:
            return False
        if self.mci.send("setaudio %s volume to %d" %
                         (self.mci.alias, self.volume * 10)):
            return True
        try:
            # 低 16 位左声道、高 16 位右声道，0..0xFFFF
            x = int(0xFFFF * self.volume / 100.0)
            dw = (x & 0xFFFF) | ((x & 0xFFFF) << 16)
            winmm = windll.winmm
            winmm.waveOutSetVolume.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            winmm.waveOutSetVolume.restype = ctypes.c_uint
            # 传 None = 该进程默认波形输出设备
            return winmm.waveOutSetVolume(None, dw) == 0
        except Exception:
            return False

    def close(self):
        if self.loaded:
            try:
                self.mci.send("stop %s" % self.mci.alias)
                self.mci.send("close %s" % self.mci.alias)
            except Exception:
                pass
        if self.wav_path and os.path.isfile(self.wav_path):
            try:
                os.remove(self.wav_path)
            except Exception:
                pass
        self.wav_path = ""
        self.source_paths = []
        self.peaks = []
        self.duration_ms = 0.0
        self.loaded = False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def guess_audio_for_chart(chart_path, log=print):
    """打开谱面时，自动探测同目录的 Inst / Voices / 同名音频。"""
    if not chart_path or not os.path.isfile(chart_path):
        return []
    d = os.path.dirname(os.path.abspath(chart_path))
    base = os.path.splitext(os.path.basename(chart_path))[0]
    # 去掉 -easy/-hard/-normal 之类的难度后缀
    for suf in ("-hard", "-easy", "-normal", "-erect", "_hard", "_easy"):
        if base.lower().endswith(suf):
            base = base[:-len(suf)]
            break
    inst = voices = ""
    cands_inst = [os.path.join(d, n) for n in
                  ("Inst.ogg", "inst.ogg", "Inst.mp3", "inst.mp3", "Inst.wav",
                   "song.ogg", "Song.ogg")]
    cands_inst += [os.path.join(d, base + e) for e in AUDIO_EXTS]
    for p in cands_inst:
        if os.path.isfile(p):
            inst = p
            break
    for n in ("Voices.ogg", "voices.ogg", "Voices.mp3", "voices.mp3"):
        p = os.path.join(d, n)
        if os.path.isfile(p):
            voices = p
            break
    res = [p for p in (inst, voices) if p]
    if not res:
        # 兜底：同目录任意音频文件
        for n in sorted(os.listdir(d)):
            if n.lower().endswith(AUDIO_EXTS):
                res = [os.path.join(d, n)]
                break
    return res
