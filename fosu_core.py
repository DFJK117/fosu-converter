# -*- coding: utf-8 -*-
"""
fosu_core - FNF (PsychEngine) -> osu!mania beatmap conversion core
无第三方依赖，标准库实现。GUI / 命令行共用。
"""
import json
import os
import sys
import math
import zipfile
import shutil
import subprocess

# Windows 下子进程不弹黑窗
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


# ---------------------------------------------------------------- 读取与检测

def load_chart(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # PsychEngine 难度文件：{"song": {...}}；也兼容直接就是 song 对象
    if "song" in data and isinstance(data["song"], dict) and "notes" in data["song"]:
        return data["song"]
    if "notes" in data:
        return data
    raise ValueError("无法识别的谱面格式：找不到 song.notes")


def iter_sections(song: Dict[str, Any]):
    for sec in song.get("notes", []):
        yield sec


def detect_layout(song: Dict[str, Any], force_keys: int = 0) -> Dict[str, Any]:
    """
    检测键数与两侧布局。
    PsychEngine: 对手 lane = 0 .. keys-1；BF lane = keys .. 2*keys-1
    普通 4K 谱面没有 mania 字段，lane 0-3 对手 / 4-7 BF。
    返回 dict: keys, bf_start, mania_field, max_lane, used_lanes, warnings
    force_keys>0 时手动指定每侧键数。
    """
    warnings = []
    mania_field = song.get("mania")
    max_lane = -1
    used = set()
    for sec in iter_sections(song):
        for n in sec.get("sectionNotes", []):
            if len(n) >= 2 and isinstance(n[1], int) and n[1] >= 0:
                used.add(n[1])
                max_lane = max(max_lane, n[1])

    if max_lane < 0:
        raise ValueError("谱面里没有任何音符")

    # 按实际使用的最大轨道推断：两侧对称时 BF 从 keys 开始
    derived = max(4, math.ceil((max_lane + 1) / 2)) if max_lane >= 4 else max_lane + 1

    if force_keys and force_keys > 0:
        keys = force_keys
        if keys != derived:
            warnings.append(f"手动指定 {keys}K（自动检测为 {derived}K）")
    else:
        if isinstance(mania_field, int) and mania_field >= 4:
            if mania_field == derived:
                keys = mania_field
            else:
                # 本谱就是这种情况：mania=6 但实际写了 7 轨，以实际数据为准
                keys = derived
                warnings.append(
                    f"mania 字段={mania_field}，但谱面实际使用 {derived} 轨/侧，"
                    f"已按实际轨道转换（字段可能是遗留值）")
        else:
            keys = derived

    bf_start = keys
    return {
        "keys": keys,
        "bf_start": bf_start,
        "mania_field": mania_field,
        "max_lane": max_lane,
        "used_lanes": sorted(used),
        "warnings": warnings,
    }


def is_damage_note(n: List[Any]) -> bool:
    """
    PsychEngine 扣血音符：note 数组第 4 个元素（noteType）== 3。
    新版引擎也可能写字符串类型（hurt/damage/hazard）。
    """
    if len(n) <= 3 or n[3] is None:
        return False
    typ = n[3]
    if isinstance(typ, (int, float)):
        return int(typ) == 3
    if isinstance(typ, str):
        low = typ.lower()
        return low == "3" or any(k in low for k in ("hurt", "damage", "hazard", "mine"))
    return False


def note_belongs_to_bf(must_hit: bool, lane: int, keys: int) -> bool:
    """
    与 theleername/mania-converter 的 Side=1 规则完全一致：
      BF 音符 = (mustHitSection 且 lane<keys) 或 (非 mustHitSection 且 lane>=keys)
    FNF 中玩家焦点小节 BF 音符写在低轨道，对手焦点小节写在高轨道。
    """
    if must_hit:
        return lane < keys
    return lane >= keys


def collect_notes(song: Dict[str, Any], side: str = "bf",
                  keys: Optional[int] = None,
                  filter_damage: bool = True,
                  sensitivity: float = 0.0) -> List[Dict[str, Any]]:
    """
    提取音符。
    side: "bf" 只取玩家要打的音符（默认，参考站 Side=1）；
          "opp" 只取对手音符（Side=2）；"all" 全部（Side=0，同列合并）。
    返回 [{time, lane(0..keys-1), side:'bf'/'opp', length}]，按时间排序。
    sensitivity: 同列相邻音符时间差 <= 该毫秒数时视为重复（参考站去重逻辑）。
    """
    if keys is None:
        keys = detect_layout(song)["keys"]
    out: List[Dict[str, Any]] = []
    last_time: Dict[int, float] = {}
    damage_skipped = 0
    for sec in iter_sections(song):
        must_hit = bool(sec.get("mustHitSection", False))
        for n in sec.get("sectionNotes", []):
            if len(n) < 2:
                continue
            t = float(n[0])
            lane = n[1]
            if not isinstance(lane, int) or lane < 0 or lane >= 2 * keys:
                continue  # 相机/事件音符或越界
            if filter_damage and is_damage_note(n):
                damage_skipped += 1
                continue
            is_bf = note_belongs_to_bf(must_hit, lane, keys)
            if side == "bf" and not is_bf:
                continue
            if side == "opp" and is_bf:
                continue
            rel = lane - keys if lane >= keys else lane
            length = float(n[2]) if len(n) >= 3 and n[2] else 0.0
            # 参考站去重：同列、与上一个音符时间差在灵敏度内 -> 丢弃
            prev = last_time.get(rel)
            if prev is not None and abs(t - prev) <= sensitivity:
                continue
            last_time[rel] = t
            out.append({"time": t, "lane": rel,
                        "side": "bf" if is_bf else "opp", "length": length})
    out.sort(key=lambda x: (x["time"], x["lane"]))
    return out


def count_damage_notes(song: Dict[str, Any]) -> int:
    return sum(1 for sec in iter_sections(song)
               for n in sec.get("sectionNotes", []) if is_damage_note(n))


def chart_stats(song: Dict[str, Any], keys: int) -> Dict[str, int]:
    """供界面显示：BF/对手/扣血音符数量（BF 按 mustHitSection 规则）。"""
    bf = opp = damage = 0
    for sec in iter_sections(song):
        must_hit = bool(sec.get("mustHitSection", False))
        for n in sec.get("sectionNotes", []):
            if len(n) < 2 or not isinstance(n[1], int) or n[1] < 0 \
                    or n[1] >= 2 * keys:
                continue
            if is_damage_note(n):
                damage += 1
                continue
            if note_belongs_to_bf(must_hit, n[1], keys):
                bf += 1
            else:
                opp += 1
    return {"bf": bf, "opp": opp, "damage": damage}


def _raw_change_points(song: Dict[str, Any]) -> List[Dict[str, float]]:
    """
    按 section 的 changeBPM/bpm 字段与 lengthInSteps 累加，得到“名义”变速点。
    注意：部分谱面（尤其魔改引擎）这些字段是遗留垃圾值，必须再用音符时间校验。
    """
    base_bpm = float(song.get("bpm", 100))
    points = [{"time": 0.0, "bpm": base_bpm}]
    cur_time = 0.0
    cur_bpm = base_bpm
    sections = song.get("notes", [])
    for idx, sec in enumerate(sections):
        new_bpm = None
        if isinstance(sec.get("changeBPM"), (int, float)) and sec["changeBPM"] > 0:
            new_bpm = float(sec["changeBPM"])
        elif isinstance(sec.get("bpm"), (int, float)) and sec["bpm"] > 0 and idx > 0:
            new_bpm = float(sec["bpm"])
        if new_bpm is not None and abs(new_bpm - cur_bpm) > 1e-6:
            points.append({"time": cur_time, "bpm": new_bpm})
            cur_bpm = new_bpm
        steps = sec.get("lengthInSteps", 16)
        if not steps and idx < len(song.get("sectionLengths", [])):
            steps = song["sectionLengths"][idx]
        cur_time += (60000.0 / cur_bpm) * (steps / 4.0)  # 1 step = 1/4 beat
    return points


def _grid_residual(t: float, bpm: float, div: int = 16) -> float:
    grid = 60000.0 / bpm / div
    return abs(t / grid - round(t / grid)) * grid


def detect_timing(song: Dict[str, Any]) -> Dict[str, Any]:
    """
    用音符绝对时间校验名义变速点：
    - 若全部音符都落在基础 BPM 的 1/16 拍网格上（容差 2ms），
      说明 changeBPM 字段无效（常见于魔改 Shaggy 谱），只用单一定速点；
    - 否则采用分段变速点，并逐段验证、剔除不符的点。
    返回 {"points":[...], "warnings":[...]}
    """
    warns: List[str] = []
    base_bpm = float(song.get("bpm", 100))
    times = [float(n[0]) for sec in iter_sections(song)
             for n in sec.get("sectionNotes", []) if len(n) >= 2]
    if not times:
        return {"points": [{"time": 0.0, "bpm": base_bpm}], "warnings": warns}
    last_t = max(times)

    const_bad = sum(1 for t in times if _grid_residual(t, base_bpm) > 2.0)
    raw = _raw_change_points(song)
    real_changes = [p for p in raw[1:] if p["time"] <= last_t + 60000.0 / base_bpm]

    if not real_changes or const_bad <= max(1, int(len(times) * 0.005)):
        if real_changes:
            warns.append(
                f"忽略 {len(real_changes)} 个 changeBPM 字段：全部 {len(times)} 个音符"
                f"都对齐基础 {base_bpm:g} BPM 网格（字段为遗留值）")
        return {"points": [{"time": 0.0, "bpm": base_bpm}], "warnings": warns}

    # 逐段验证变速点：该段音符必须贴合新 BPM 网格才采纳
    pts = [{"time": 0.0, "bpm": base_bpm}]
    cur_bpm = base_bpm
    for p in raw[1:]:
        if p["time"] > last_t + 60000.0 / cur_bpm:
            break
        seg = [t for t in times if p["time"] <= t < p["time"] + 60000.0 / p["bpm"] * 4]
        fit_new = sum(1 for t in seg if _grid_residual(t - p["time"], p["bpm"]) <= 2.0)
        fit_old = sum(1 for t in seg if _grid_residual(t, cur_bpm) <= 2.0)
        if seg and fit_new >= fit_old and fit_new / len(seg) > 0.9:
            pts.append({"time": p["time"], "bpm": p["bpm"]})
            cur_bpm = p["bpm"]
        else:
            warns.append(f"变速点 {p['time']:.0f}ms/{p['bpm']:g}BPM 未通过音符校验，已忽略")
    return {"points": pts, "warnings": warns}


# ---------------------------------------------------------------- .osu 生成

def column_x(col: int, keys: int) -> int:
    # 与 mania-converter 的 convertNoteToOsu 完全一致：列区间中点
    w = 512.0 / keys
    return int((w * col + (w * (col + 1) - 1)) / 2) + 1


def safe_name(name: str) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip().rstrip(".") or "unknown"


def prettify(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").strip().title()


@dataclass
class ConvertOptions:
    side: str = "bf"                # "bf"=玩家音符 / "opp"=对手 / "all"=全部
    filter_damage: bool = True      # 过滤扣血音符（noteType 3）
    dedupe_sensitivity: float = 0.0  # 同列重复音符判定毫秒
    keys_override: int = 0          # 0 = 自动
    version: str = "Easy"           # 难度名
    hp: float = 4.0
    od: float = 4.0
    slider_multiplier: float = 1.4
    offset_ms: float = 0.0          # 全局提前(负)/延后(正)
    artist: str = "Vs Shaggy"
    creator: str = "fosu"
    audio_filename: str = "audio.ogg"
    title_override: str = ""
    tags: str = "fnf psychengine mania converted fosu"

    @property
    def bf_only(self) -> bool:     # 兼容旧调用
        return self.side == "bf"


def build_osu(song: Dict[str, Any], opts: ConvertOptions,
              layout: Optional[Dict] = None) -> str:
    if layout is None:
        layout = detect_layout(song, opts.keys_override)
    keys = layout["keys"]
    notes = collect_notes(song, side=opts.side, keys=keys,
                          filter_damage=opts.filter_damage,
                          sensitivity=opts.dedupe_sensitivity)
    if not notes:
        raise ValueError("筛选后没有任何可转换的音符（检查侧边/扣血音符设置）")
    timing = detect_timing(song)
    timing_points = timing["points"]

    title = opts.title_override or prettify(str(song.get("song", "unknown")))
    preview = max(0, int(notes[0]["time"] - 2000))

    lines = []
    lines.append("osu file format v14")
    lines.append("")
    lines.append("[General]")
    lines.append(f"AudioFilename: {opts.audio_filename}")
    lines.append("AudioLeadIn: 0")
    lines.append(f"PreviewTime: {preview}")
    lines.append("Countdown: 0")
    lines.append("SampleSet: Normal")
    lines.append("StackLeniency: 0.7")
    lines.append("Mode: 3")
    lines.append("LetterboxInBreaks: 0")
    lines.append("SpecialStyle: 0")
    lines.append("WidescreenStoryboard: 0")
    lines.append("")
    lines.append("[Editor]")
    lines.append("DistanceSpacing: 1")
    lines.append("BeatDivisor: 4")
    lines.append("GridSize: 4")
    lines.append("TimelineZoom: 1.8")
    lines.append("")
    lines.append("[Metadata]")
    lines.append(f"Title:{title}")
    lines.append(f"TitleUnicode:{title}")
    lines.append(f"Artist:{opts.artist}")
    lines.append(f"ArtistUnicode:{opts.artist}")
    lines.append(f"Creator:{opts.creator}")
    lines.append(f"Version:{opts.version}")
    lines.append("Source:Friday Night Funkin'")
    lines.append(f"Tags:{opts.tags}")
    lines.append("BeatmapID:0")
    lines.append("BeatmapSetID:-1")
    lines.append("")
    lines.append("[Difficulty]")
    lines.append(f"HPDrainRate:{opts.hp}")
    lines.append(f"CircleSize:{keys}")
    lines.append(f"OverallDifficulty:{opts.od}")
    lines.append(f"ApproachRate:{opts.od}")
    lines.append(f"SliderMultiplier:{opts.slider_multiplier}")
    lines.append("SliderTickRate:1")
    lines.append("")
    lines.append("[Events]")
    lines.append("//Background and Video events")
    lines.append("//Break Periods")
    lines.append("//Storyboard Layer 0 (Background)")
    lines.append("//Storyboard Layer 1 (Fail)")
    lines.append("//Storyboard Layer 2 (Pass)")
    lines.append("//Storyboard Layer 3 (Foreground)")
    lines.append("//Storyboard Sound Samples")
    lines.append("")
    lines.append("[TimingPoints]")
    for p in timing_points:
        t = round(p["time"] + opts.offset_ms)
        beat = 60000.0 / p["bpm"]
        lines.append(f"{t},{beat:.6f},4,0,0,80,1,0")
    lines.append("")
    lines.append("[Colours]")
    # 经典 osu!mania 7K 居中轨道不需要额外颜色；4K/7K osu 自动配色
    lines.append("")
    lines.append("[HitObjects]")
    for n in notes:
        x = column_x(n["lane"], keys)
        t = int(math.floor(n["time"] + opts.offset_ms))
        if n["length"] > 0:
            end = int(math.floor(n["time"] + n["length"] + opts.offset_ms))
            lines.append(f"{x},192,{t},128,0,{end},0:0:0:0:")
        else:
            lines.append(f"{x},192,{t},1,0,0:0:0:0:")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 音频混音

def find_ffmpeg() -> str:
    """打包后 ffmpeg.exe 与 exe 同目录或在 _MEIPIP 内；开发时取 fosu-build/bin。"""
    here = os.path.dirname(os.path.abspath(__file__))
    meipass = getattr(sys, "_MEIPASS", here)
    candidates = [
        os.path.join(meipass, "ffmpeg.exe"),
        os.path.join(here, "ffmpeg.exe"),
        os.path.join(here, "bin", "ffmpeg.exe"),
        os.path.join(here, "..", "ffmpeg-master-latest-win64-gpl", "bin", "ffmpeg.exe"),
        "ffmpeg",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "ffmpeg"


def _run_ffmpeg(cmd: List[str], log=print) -> None:
    """运行 ffmpeg；stderr 重定向到临时日志（管道在部分宿主环境会导致进程被杀）。"""
    import tempfile
    log("ffmpeg " + " ".join(f'"{c}"' if " " in c else c for c in cmd[1:]))
    with tempfile.NamedTemporaryFile("wb", suffix=".log", delete=False) as lf:
        log_path = lf.name
    out_path = cmd[-1]
    try:
        last_err = ""
        for attempt in range(3):
            with open(log_path, "wb") as lf:
                r = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=lf,
                                   creationflags=_NO_WINDOW)
            if r.returncode == 0 and os.path.isfile(out_path) and \
                    os.path.getsize(out_path) > 1024:
                return
            try:
                with open(log_path, "rb") as f:
                    last_err = f.read().decode("utf-8", "replace")[-2000:]
            except OSError:
                pass
            log(f"⚠ ffmpeg 第 {attempt + 1} 次未成功 (code {r.returncode})，重试…")
        raise RuntimeError(f"ffmpeg 失败 (code {r.returncode}):\n{last_err}")
    finally:
        try:
            os.remove(log_path)
        except OSError:
            pass


def _probe_duration(path: str, ffmpeg: str) -> float:
    import re
    import tempfile
    fd, lp = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    try:
        with open(lp, "wb") as lf:
            subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-i", path],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=lf, creationflags=_NO_WINDOW)
        with open(lp, "rb") as f:
            txt = f.read().decode("utf-8", "replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
        if not m:
            return 0.0
        h, mm, ss = m.groups()
        return int(h) * 3600 + int(mm) * 60 + float(ss)
    finally:
        try:
            os.remove(lp)
        except OSError:
            pass


def _chunked_encode(ffmpeg: str, inputs: List, out_path: str, log=print,
                    chunk: float = 20.0) -> None:
    """
    inputs=[(path, volume), ...]：多轨混音或单轨转码。
    分片编码（每片 20s，输出端 seek，采样精确），再 concat copy 合并，
    规避部分宿主环境对单个长时 ffmpeg 进程的限制。
    """
    import tempfile
    tmp = tempfile.mkdtemp(prefix="fosu_ff_")
    try:
        dur = max(_probe_duration(p, ffmpeg) for p, _ in inputs)
        if dur <= 0:
            raise RuntimeError("无法读取音频时长")
        n = int(math.ceil(dur / chunk))
        log(f"音频 {dur:.1f}s，分 {n} 片编码…")
        chunks = []
        for i in range(n):
            start = i * chunk
            ch = os.path.join(tmp, f"c{i:03d}.mka")
            cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
            for p, _ in inputs:
                cmd += ["-i", p]
            fc = []
            for j, (_, v) in enumerate(inputs):
                fc.append(f"[{j}:a]volume={v:.3f},"
                          f"aformat=sample_fmts=fltp:sample_rates=44100:"
                          f"channel_layouts=stereo[a{j}]")
            if len(inputs) > 1:
                labels = "".join(f"[a{j}]" for j in range(len(inputs)))
                fc.append(f"{labels}amix=inputs={len(inputs)}:duration=longest:"
                          f"normalize=0[mx]")
                src = "[mx]"
            else:
                src = "[a0]"
            fc.append(f"{src}alimiter=limit=0.95[a]")
            cmd += ["-filter_complex", ";".join(fc), "-map", "[a]",
                    "-ss", f"{start:.3f}", "-t", f"{chunk:.3f}",
                    "-c:a", "libvorbis", "-q:a", "5", ch]
            _run_ffmpeg(cmd, log)
            if os.path.isfile(ch) and os.path.getsize(ch) > 256:
                chunks.append(ch)
        if not chunks:
            raise RuntimeError("音频分片编码全部失败")
        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for ch in chunks:
                f.write("file '" + ch.replace(os.sep, "/") + "'\n")
        merged = os.path.join(tmp, "merged.mka")
        _run_ffmpeg([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                     "-y", "-f", "concat", "-safe", "0", "-i", lst,
                     "-c", "copy", merged], log)
        _run_ffmpeg([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                     "-y", "-i", merged, "-c", "copy", out_path], log)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def mix_audio(inst: str, voices: Optional[str], out_path: str,
              vol_inst: float = 1.0, vol_voices: float = 1.0,
              ffmpeg: Optional[str] = None, log=print) -> None:
    ffmpeg = ffmpeg or find_ffmpeg()
    inputs = [(inst, vol_inst)]
    if voices and os.path.isfile(voices):
        inputs.append((voices, vol_voices))
    log(f"混音 {len(inputs)} 轨 -> {os.path.basename(out_path)}")
    _chunked_encode(ffmpeg, inputs, out_path, log)


def transcode_audio(src: str, out_path: str, volume: float = 1.0,
                    ffmpeg: Optional[str] = None, log=print) -> None:
    ffmpeg = ffmpeg or find_ffmpeg()
    log(f"转码 -> {os.path.basename(out_path)}")
    _chunked_encode(ffmpeg, [(src, volume)], out_path, log)


# ---------------------------------------------------------------- 打包 osz

def package_osz(set_dir: str, osz_path: str) -> None:
    if os.path.exists(osz_path):
        os.remove(osz_path)
    with zipfile.ZipFile(osz_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(set_dir):
            for fn in files:
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, set_dir).replace(os.sep, "/")
                z.write(full, arc)


def convert_all(chart_path: str, out_osz: str, opts: ConvertOptions,
                inst_path: Optional[str] = None, voices_path: Optional[str] = None,
                mix: bool = True, vol_inst: float = 1.0, vol_voices: float = 1.0,
                ffmpeg: Optional[str] = None, log=print,
                keep_workdir: bool = False) -> Dict[str, Any]:
    song = load_chart(chart_path)
    layout = detect_layout(song, opts.keys_override)
    for w in layout["warnings"]:
        log("⚠ " + w)
    for w in detect_timing(song)["warnings"]:
        log("⚠ " + w)
    keys = layout["keys"]
    stats = chart_stats(song, keys)
    log(f"检测：mania 字段={layout['mania_field']}，实际 {keys}K/侧，"
        f"BF 音符 {stats['bf']}，对手音符 {stats['opp']}，"
        f"扣血音符 {stats['damage']}")
    if opts.filter_damage and stats["damage"]:
        log(f"已按设置过滤 {stats['damage']} 个扣血音符（noteType 3）")
    chosen = collect_notes(song, side=opts.side, keys=keys,
                           filter_damage=opts.filter_damage,
                           sensitivity=opts.dedupe_sensitivity)
    if not chosen:
        raise ValueError("筛选后没有任何可转换的音符")
    holds = sum(1 for n in chosen if n["length"] > 0)
    side_cn = {"bf": "BF(玩家)", "opp": "对手", "all": "全部"}[opts.side]
    log(f"本次导出【{side_cn}】音符 {len(chosen)} 个，其中长按 {holds}，"
        f"时间 {chosen[0]['time']:.0f}~{chosen[-1]['time']:.0f} ms")

    work = os.path.join(os.path.dirname(out_osz),
                        ".fosu_work_" + safe_name(os.path.splitext(os.path.basename(out_osz))[0]))
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work, exist_ok=True)

    # 音频
    if mix and inst_path and os.path.isfile(inst_path):
        audio_out = os.path.join(work, "audio.ogg")
        log("开始混音 Inst + Voices ...")
        mix_audio(inst_path, voices_path, audio_out, vol_inst, vol_voices,
                  ffmpeg=ffmpeg, log=log)
        log("混音完成 -> audio.ogg")
    else:
        src = inst_path or voices_path
        if not src or not os.path.isfile(src):
            raise FileNotFoundError("找不到音频文件")
        audio_out = os.path.join(work, "audio.ogg")
        transcode_audio(src, audio_out, vol_inst, ffmpeg=ffmpeg, log=log)

    # 谱面
    osu = build_osu(song, opts, layout)
    title = opts.title_override or prettify(str(song.get("song", "unknown")))
    osu_name = safe_name(f"{opts.artist} - {title} [{opts.version}].osu")
    with open(os.path.join(work, osu_name), "w", encoding="utf-8") as f:
        f.write(osu)
    log("谱面已生成 -> " + osu_name)

    package_osz(work, out_osz)
    log("打包完成 -> " + out_osz)
    if not keep_workdir:
        shutil.rmtree(work, ignore_errors=True)
    return {"layout": layout, "stats": stats, "exported": len(chosen),
            "holds": holds, "osz": out_osz}
