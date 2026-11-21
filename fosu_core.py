# -*- coding: utf-8 -*-
"""
fosu_core - FNF (PsychEngine/Kade/旧版官方) -> osu!mania beatmap conversion core
无第三方依赖，标准库实现。GUI / 命令行共用。
"""
import json
import os
import sys
import math
import random
import zipfile
import shutil
import subprocess

# Windows 下子进程不弹黑窗
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


# ---------------------------------------------------------------- 读取与检测

# 旧版 FNF chart 归一化：内部统一为「PE 新版」结构
#   sectionNotes: [time(float ms), lanes(List[int]), sustain(float ms), noteType(可选)]
# 兼容的旧版差异：
#   - 外壳 {"song": {...}} / 顶层 {"notes": ...}
#   - noteData 为 int（旧版官方 ≤0.2.x / Kade）或 Array（新版和弦）
#   - 时间是字符串（老编辑器导出）
#   - 老 Kade 0.x 顶层 sections 数组代替 mustHitSection（0=玩家侧）
#   - Kade 系 altAnim、PE 系 gfSection 等字段保留不参与转换

ENGINE_CN = {
    "kade": "Kade Engine 系（altAnim 字段）",
    "psych": "Psych Engine",
    "official-legacy": "旧版官方 FNF（≤0.2.x，noteData 为整数）",
    "official-new": "新版官方 / PE 新版（noteData 支持数组）",
    "unknown": "未知引擎",
}


def _norm_lanes(v) -> List[int]:
    """noteData 归一成 int 列表：int / float / 数字字符串 / Array 混吃。"""
    if v is None or isinstance(v, bool):
        return []
    if isinstance(v, int):
        return [v]
    if isinstance(v, float):
        return [int(v)]
    if isinstance(v, str):
        try:
            return [int(float(v))]
        except ValueError:
            return []
    if isinstance(v, (list, tuple)):
        out: List[int] = []
        for x in v:
            if isinstance(x, bool):
                continue
            if isinstance(x, (int, float)):
                out.append(int(x))
            elif isinstance(x, str):
                try:
                    out.append(int(float(x)))
                except ValueError:
                    pass
        return out
    return []


def note_lanes(note: List[Any]) -> List[int]:
    """取一个 sectionNotes 元素的有效轨道列表（归一化后恒为 list）。"""
    if len(note) < 2:
        return []
    return _norm_lanes(note[1])


def _extract_song(data: Dict[str, Any]) -> Dict[str, Any]:
    if "song" in data and isinstance(data["song"], dict) and "notes" in data["song"]:
        return data["song"]
    if "notes" in data:
        return data
    raise ValueError("无法识别的谱面格式：找不到 song.notes")


def normalize_song(song: Dict[str, Any]) -> Dict[str, Any]:
    """原地把 song 归一化成内部格式，返回探测信息。"""
    info: Dict[str, Any] = {"engine": "unknown", "fixed": [], "warnings": [],
                            "saw_int": 0, "saw_list": 0, "saw_str_time": 0}
    notes = song.get("notes")
    if not isinstance(notes, list):
        raise ValueError("谱面 notes 字段缺失或不是数组")

    # 老 Kade 0.x：顶层 sections 数组（0=玩家小节，1=对手小节）
    top_sections = song.get("sections")
    if isinstance(top_sections, int):
        top_sections = [top_sections]

    saw_alt = saw_gf = saw_events = saw_valid = 0
    inferred = 0
    for i, sec in enumerate(notes):
        if not isinstance(sec, dict):
            continue
        sn = sec.get("sectionNotes")
        if not isinstance(sn, list):
            sec["sectionNotes"] = []
            continue
        new_sn = []
        for n in sn:
            if not isinstance(n, (list, tuple)) or len(n) < 2:
                continue
            t = n[0]
            if isinstance(t, str):
                try:
                    t = float(t)
                    info["saw_str_time"] += 1
                except ValueError:
                    continue
            try:
                t = float(t)
            except (TypeError, ValueError):
                continue
            lanes = _norm_lanes(n[1])
            if not lanes:
                continue  # 事件/相机音符（noteData 非数字）
            if isinstance(n[1], int):
                info["saw_int"] += 1
            elif isinstance(n[1], (list, tuple)):
                info["saw_list"] += 1
            sust = 0.0
            if len(n) >= 3 and n[2] is not None:
                try:
                    sust = float(n[2])
                except (TypeError, ValueError):
                    sust = 0.0
            nn: List[Any] = [t, lanes, sust]
            if len(n) >= 4 and n[3] is not None:
                nn.append(n[3])  # noteType（扣血等）原样保留
            new_sn.append(nn)
        sec["sectionNotes"] = new_sn
        if "mustHitSection" not in sec:
            if isinstance(top_sections, list) and i < len(top_sections):
                # 老 Kade 语义：0 = 玩家侧小节
                sec["mustHitSection"] = (top_sections[i] == 0)
                inferred += 1
            else:
                # Haxe Bool 默认 false：未标记的小节按对手侧，并提示
                sec["mustHitSection"] = False
                info["warnings"].append(
                    f"第 {i + 1} 小节缺少 mustHitSection，已按引擎默认"
                    f"（对手侧）处理；若实际是玩家侧，请用「玩家↔对手互换」修正")
        if sec.get("altAnim"):
            saw_alt += 1
        if sec.get("gfSection"):
            saw_gf += 1
    if song.get("events"):
        saw_events += 1
    if "validScore" in song:
        saw_valid += 1

    if inferred:
        info["fixed"].append(
            f"{inferred} 个小节用顶层 sections 数组推断 mustHitSection"
            f"（老 Kade 0.x 语义：0=玩家侧）")
    if info["saw_str_time"]:
        info["fixed"].append(f"{info['saw_str_time']} 个音符时间是字符串，已转为数值")

    # 引擎探测
    if saw_alt:
        info["engine"] = "kade"
    elif saw_gf or saw_events or saw_valid:
        info["engine"] = "psych"
    elif info["saw_int"] and not info["saw_list"]:
        info["engine"] = "official-legacy"
    elif info["saw_list"]:
        info["engine"] = "official-new"
    return info


def normalize_chart_data(data: Dict[str, Any]):
    """从已解析的 JSON dict 取 song 并归一化，返回 (song, info)。

    这是归一化的唯一入口（重复调用 normalize_song 会导致引擎探测误判，
    因为归一化后 noteData 全部变成 list）。"""
    song = _extract_song(data)
    info = normalize_song(song)
    return song, info


def load_chart_data(data: Dict[str, Any]) -> Dict[str, Any]:
    song, _ = normalize_chart_data(data)
    return song


def load_chart_info(path: str):
    """读谱面并返回 (song, info)。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return normalize_chart_data(data)


def load_chart(path: str) -> Dict[str, Any]:
    song, _ = load_chart_info(path)
    return song


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
            for lane in note_lanes(n):
                used.add(lane)
                max_lane = max(max_lane, lane)

    if max_lane < 0:
        raise ValueError("谱面里没有任何音符")

    # 按实际使用的最大轨道推断：两侧对称时 BF 从 keys 开始
    derived = max(4, math.ceil((max_lane + 1) / 2)) if max_lane >= 4 else max_lane + 1

    if force_keys and force_keys > 0:
        keys = force_keys
        if keys != derived:
            warnings.append(f"手动指定 {keys}K（自动检测为 {derived}K）")
    else:
        if isinstance(mania_field, int) and mania_field >= 0:
            # FNF 多键语法：keys = mania + 1（mania=0 即 1K，mania=6 即 7K）
            mk = mania_field + 1
            if mk >= 1:
                if mk == derived:
                    keys = mk
                else:
                    # mania 字段与实际轨道不符：以实际数据为准（常见于遗留谱）
                    keys = derived
                    warnings.append(
                        f"mania 字段={mania_field}（={mk}K），但谱面实际使用 "
                        f"{derived} 轨/侧，已按实际轨道转换（字段可能是遗留值）")
            else:
                keys = derived
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
            lanes = note_lanes(n)
            if not lanes:
                continue
            t = float(n[0])
            if filter_damage and is_damage_note(n):
                damage_skipped += 1
                continue
            for lane in lanes:
                if lane < 0 or lane >= 2 * keys:
                    continue  # 越界（相机/事件音符）
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
            lanes = note_lanes(n)
            if not lanes:
                continue
            if is_damage_note(n):
                damage += len(lanes)
                continue
            for lane in lanes:
                if lane < 0 or lane >= 2 * keys:
                    continue
                if note_belongs_to_bf(must_hit, lane, keys):
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
    video_path: str = ""           # 源视频（MP4 等）路径，可选
    video_name: str = "video.mp4"  # 打进 osz 内的视频文件名

    @property
    def bf_only(self) -> bool:     # 兼容旧调用
        return self.side == "bf"


def build_osu(song: Dict[str, Any], opts: ConvertOptions,
              layout: Optional[Dict[str, Any]] = None) -> str:
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
    if opts.video_path:
        # osu! 视频事件：Video,开始时间,"文件名"（文件需在 osz 内）
        lines.append(f'Video,0,"{opts.video_name}"')
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
    """打包后 ffmpeg.exe 在 _MEIPASS 内、或与 exe 同目录；开发时取脚本目录/bin。"""
    here = os.path.dirname(os.path.abspath(__file__))
    meipass = getattr(sys, "_MEIPASS", here)
    candidates = [os.path.join(meipass, "ffmpeg.exe")]
    if getattr(sys, "frozen", False):
        # onefile：也认「exe 旁边放一个 ffmpeg.exe」这种用法
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.join(exe_dir, "ffmpeg.exe"))
    candidates += [
        os.path.join(here, "ffmpeg.exe"),
        os.path.join(here, "bin", "ffmpeg.exe"),
        os.path.join(here, "..", "ffmpeg-master-latest-win64-gpl", "bin", "ffmpeg.exe"),
        "ffmpeg",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "ffmpeg"


SIDE_CN = {"bf": "BF(玩家)", "opp": "对手", "all": "全部"}


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


def _has_audio_stream(path: str, ffmpeg: str) -> bool:
    """探测文件是否带音轨（视频当音频源前先查，免得 ffmpeg 报 cryptic 错）。"""
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
        return bool(re.search(r"Stream #\d+.*: Audio:", txt))
    finally:
        try:
            os.remove(lp)
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
    log(f"本次导出【{SIDE_CN.get(opts.side, opts.side)}】音符 {len(chosen)} 个，其中长按 {holds}，"
        f"时间 {chosen[0]['time']:.0f}~{chosen[-1]['time']:.0f} ms")

    work = os.path.join(os.path.dirname(out_osz),
                        ".fosu_work_" + safe_name(os.path.splitext(os.path.basename(out_osz))[0]))
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work, exist_ok=True)

    # 音频：Inst(+Voices) 混音；都没有时退回视频音轨
    if ffmpeg is None:
        ffmpeg = find_ffmpeg()
    if mix and inst_path and os.path.isfile(inst_path):
        audio_out = os.path.join(work, "audio.ogg")
        log("开始混音 Inst + Voices ...")
        mix_audio(inst_path, voices_path, audio_out, vol_inst, vol_voices,
                  ffmpeg=ffmpeg, log=log)
        log("混音完成 -> audio.ogg")
    else:
        src = inst_path or voices_path or (opts.video_path if opts.video_path else "")
        if not src or not os.path.isfile(src):
            # v5.1：允许无音频导出（无声 osz）。谱面照常打包，osu 里 AudioFilename
            # 指向的 audio.ogg 缺失只会静音，不影响谱面可用。
            log("⚠ 未找到任何音频：导出无声 osz（谱面正常；需要声音时把 audio.ogg "
                "补进 osz 即可）")
            audio_out = None
        else:
            audio_out = os.path.join(work, "audio.ogg")
            if not inst_path and not voices_path:
                log(f"未提供音频，改用视频音轨：{os.path.basename(src)}")
                if not _has_audio_stream(src, ffmpeg):
                    raise FileNotFoundError(
                        f"视频 {os.path.basename(src)} 里没有音轨，没法当音频用。\n"
                        "请改用 Inst/Voices 音频文件，或换一个带声音的视频。")
            transcode_audio(src, audio_out, vol_inst, ffmpeg=ffmpeg, log=log)

    # 谱面
    osu = build_osu(song, opts, layout)
    title = opts.title_override or prettify(str(song.get("song", "unknown")))
    osu_name = safe_name(f"{opts.artist} - {title} [{opts.version}].osu")
    with open(os.path.join(work, osu_name), "w", encoding="utf-8") as f:
        f.write(osu)
    log("谱面已生成 -> " + osu_name)

    # 视频（MP4）：原样打进 osz，并在 .osu 里写 Video 事件
    if opts.video_path:
        if not os.path.isfile(opts.video_path):
            raise FileNotFoundError(f"找不到视频文件：{opts.video_path}")
        shutil.copyfile(opts.video_path, os.path.join(work, opts.video_name))
        log(f"视频已打入 -> {opts.video_name}")

    package_osz(work, out_osz)
    log("打包完成 -> " + out_osz)
    if not keep_workdir:
        shutil.rmtree(work, ignore_errors=True)
    return {"layout": layout, "stats": stats, "exported": len(chosen),
            "holds": holds, "osz": out_osz}


# ---------------------------------------------------------------- 键数互转 (FNF N-K → M-K)

def get_alg(inp: int, out: int) -> List[List[int]]:
    """
    还原 TheLeerName/mania-converter 的 getAlg(input, output)：
    返回 noteArray[原轨道] = [目标轨道, ...]。
    按【绝对轨道】分侧 —— 低半 0..inp-1 永远 BF 侧、高半 inp..2*inp-1 永远对手侧；
    目标侧同理（低 0..out-1 / 高 out..2*out-1）。不依赖 mustHitSection。
      - inp < out（升键）：每源轨 fan-out 到 1~2 个目标轨（轮询分配）；
      - inp > out（降键）：多源轨 fan-in 合并到同一目标轨。
    """
    note_array: List[List[int]] = [[] for _ in range(inp * 2)]
    if inp > out:
        key_ratio = inp // out
        # BF 侧
        num = 0
        num1 = 0
        num2 = 0
        for i in range(inp):
            if num > key_ratio - 1:
                num = 0
                num1 = i
                if num2 < out - 1:
                    num2 += 1
            note_array[num + num1] = [num2]
            num += 1
        # 对手侧
        num = 0
        num1 = 0
        num2 = out
        for i in range(inp):
            if num > key_ratio - 1:
                num = 0
                num1 = i
                if num2 < out * 2 - 1:
                    num2 += 1
            note_array[inp + num + num1] = [num2]
            num += 1
    else:
        # BF 侧
        num = 0
        for i in range(out):
            if num > inp - 1:
                num = 0
            note_array[num].append(i)
            num += 1
        # 对手侧
        num = 0
        for i in range(out):
            if num > inp - 1:
                num = 0
            note_array[inp + num].append(out + i)
            num += 1
    return note_array


def convert_keys(song: Dict[str, Any], to_keys: int, from_keys: int = 0,
                 keep_damage: bool = True, log=print) -> Dict[str, Any]:
    """
    把 FNF 谱面每侧键数从 from_keys 转到 to_keys，原地修改 song 的 sectionNotes lane。
    直接采用 mania-converter 的真算法（get_alg + 固定种子随机选目标轨），
    按绝对轨道分侧，绝不会把 BF/对手翻面。保留小节结构 / mustHitSection / 长按(length) /
    扣血音符 / 事件。返回 {"from","to","converted","warnings"}。
    """
    if to_keys < 1:
        raise ValueError("目标键数必须 >= 1")
    layout = detect_layout(song, from_keys)
    fk, tk = layout["keys"], to_keys
    if fk == tk:
        log(f"原谱面已是 {fk}K，无需转换")
        return {"from": fk, "to": tk, "converted": 0, "warnings": layout["warnings"]}
    alg = get_alg(fk, tk)
    rng = random.Random(91169)  # 与 mania-converter 同种子，结果可复现
    max_lane = fk * 2 - 1
    converted = 0
    for sec in iter_sections(song):
        for n in sec.get("sectionNotes", []):
            lanes = note_lanes(n)
            if not lanes:
                continue
            if not keep_damage and is_damage_note(n):
                continue
            new = []
            for lane in lanes:
                if lane < 0 or lane > max_lane:
                    new.append(lane)  # 事件/相机音符等越界项保持原样
                    continue
                targets = alg[lane]
                new.append(targets[rng.randrange(len(targets))] if targets else lane)
                converted += 1
            n[1] = new  # 保持 list 格式（和弦仍是和弦）
    # 写字段：PE 等读 mania（keys-1）；同时写 keyCount 兼容 mania-converter 系引擎
    if tk > 4:
        song["mania"] = tk - 1
    else:
        song.pop("mania", None)
    song["keyCount"] = tk
    log(f"键数转换：{fk}K → {tk}K，共 {converted} 个音符（mania={song.get('mania')}, keyCount={tk}）")
    return {"from": fk, "to": tk, "converted": converted, "warnings": layout["warnings"]}


def convert_keys_file(chart_path: str, out_path: str, to_keys: int,
                      from_keys: int = 0, keep_damage: bool = True,
                      log=print) -> Dict[str, Any]:
    """读 chart → 转键数 → 写回 JSON（保留原始外壳 {"song":...} 结构）。"""
    with open(chart_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    target = load_chart_data(data)   # 旧版格式在此归一化
    res = convert_keys(target, to_keys, from_keys, keep_damage, log)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return res


# ---------------------------------------------------------------- 多难度批量打包

def convert_multi(entries, out_osz: str,
                  inst_path: Optional[str] = None, voices_path: Optional[str] = None,
                  mix: bool = True, vol_inst: float = 1.0, vol_voices: float = 1.0,
                  video_path: str = "", video_name: str = "video.mp4",
                  ffmpeg: Optional[str] = None, log=print,
                  keep_workdir: bool = False) -> Dict[str, Any]:
    """
    多难度批量转换：entries = [{"chart": 路径, "opts": ConvertOptions}, ...]
    共享同一份音频/视频，生成多个难度 .osu，打包成一个 .osz。
    """
    if not entries:
        raise ValueError("没有要转换的难度")
    if ffmpeg is None:
        ffmpeg = find_ffmpeg()

    # ---- 预检每项 ----
    prepared = []
    for idx, e in enumerate(entries):
        chart, opts = e["chart"], e["opts"]
        song = load_chart(chart)
        layout = detect_layout(song, opts.keys_override)
        for w in layout["warnings"]:
            log("⚠ " + w)
        for w in detect_timing(song)["warnings"]:
            log("⚠ " + w)
        keys = layout["keys"]
        stats = chart_stats(song, keys)
        chosen = collect_notes(song, side=opts.side, keys=keys,
                               filter_damage=opts.filter_damage,
                               sensitivity=opts.dedupe_sensitivity)
        if not chosen:
            raise ValueError(
                f"第 {idx + 1} 项（{os.path.basename(chart)}）筛选后没有任何可转换的音符"
                f"（难度名 {opts.version}，检查侧边/扣血音符设置）")
        holds = sum(1 for n in chosen if n["length"] > 0)
        log(f"[{idx + 1}/{len(entries)}] {os.path.basename(chart)} → {keys}K/侧，"
            f"【{SIDE_CN.get(opts.side, opts.side)}】{len(chosen)} 音符（长按 {holds}），"
            f"难度名：{opts.version}")
        prepared.append({"chart": chart, "opts": opts, "song": song,
                         "layout": layout, "chosen": chosen, "stats": stats})

    # ---- 音频源：Inst/Voices 优先，都没有则退回视频音轨 ----
    audio_src = inst_path or voices_path or video_path
    if not audio_src or not os.path.isfile(audio_src):
        raise FileNotFoundError("找不到音频文件（Inst / Voices / 视频至少要有一个）")

    work = os.path.join(os.path.dirname(out_osz),
                        ".fosu_work_" + safe_name(os.path.splitext(os.path.basename(out_osz))[0]))
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work, exist_ok=True)

    # ---- 音频：整包共用一份 audio.ogg ----
    audio_out = os.path.join(work, "audio.ogg")
    if mix and inst_path and voices_path and \
            os.path.isfile(inst_path) and os.path.isfile(voices_path):
        log("开始混音 Inst + Voices ...")
        mix_audio(inst_path, voices_path, audio_out, vol_inst, vol_voices,
                  ffmpeg=ffmpeg, log=log)
        log("混音完成 -> audio.ogg")
    else:
        if not inst_path and not voices_path:
            log(f"未提供音频，改用视频音轨：{os.path.basename(audio_src)}")
            if not _has_audio_stream(audio_src, ffmpeg):
                raise FileNotFoundError(
                    f"视频 {os.path.basename(audio_src)} 里没有音轨，没法当音频用。\n"
                    "请改用 Inst/Voices 音频文件，或换一个带声音的视频。")
        else:
            log(f"转码音频 -> {os.path.basename(audio_src)}")
        transcode_audio(audio_src, audio_out, vol_inst, ffmpeg=ffmpeg, log=log)

    # ---- 视频（可选）：打进 osz ----
    if video_path:
        if not os.path.isfile(video_path):
            raise FileNotFoundError(f"找不到视频文件：{video_path}")
        shutil.copyfile(video_path, os.path.join(work, video_name))
        log(f"视频已打入 -> {video_name}")

    # ---- 各难度 .osu（同包内文件名去重）----
    if video_path:
        # 视频是整包共用的：同步进每个难度的 opts，build_osu 才会写 Video 事件
        for p in prepared:
            p["opts"].video_path = video_path
            p["opts"].video_name = video_name or "video.mp4"
    used = set()
    titles = set()
    for p in prepared:
        opts = p["opts"]
        osu = build_osu(p["song"], opts, p["layout"])
        title = opts.title_override or prettify(str(p["song"].get("song", "unknown")))
        titles.add(title)
        name = safe_name(f"{opts.artist} - {title} [{opts.version}].osu")
        if name.lower() in used:
            k = 2
            while safe_name(f"{opts.artist} - {title} [{opts.version}] ({k}).osu").lower() in used:
                k += 1
            name = safe_name(f"{opts.artist} - {title} [{opts.version}] ({k}).osu")
        used.add(name.lower())
        with open(os.path.join(work, name), "w", encoding="utf-8") as f:
            f.write(osu)
        log("谱面已生成 -> " + name)

    if len(titles) > 1:
        log("⚠ 各难度曲名不一致：" + "、".join(sorted(titles)) +
            "。同一 osz 在 osu! 里算同一个谱面集，建议保持同一首。")

    package_osz(work, out_osz)
    log(f"打包完成（{len(prepared)} 个难度）-> {out_osz}")
    if not keep_workdir:
        shutil.rmtree(work, ignore_errors=True)
    return {"exported": len(prepared), "osz": out_osz,
            "difficulties": [p["opts"].version for p in prepared],
            "titles": sorted(titles)}


# ---------------------------------------------------------------- 玩家↔对手互换

def swap_sides(song: Dict[str, Any], log=print) -> Dict[str, Any]:
    """
    互换玩家（BF）与对手的全部音符：lane 在两侧之间对调
    （lane < keys <-> lane >= keys），mustHitSection 保持不变 ——
    归属规则（note_belongs_to_bf）作用下 BF/对手正好整体互换。

    用于「一人唱两」场景：把原本玩家+对手对唱的谱面换成玩家独唱对手部分
    （或修正 mustHitSection 标反的谱）。
    """
    layout = detect_layout(song)
    keys = layout["keys"]
    for w in layout["warnings"]:
        log("⚠ " + w)
    n = 0
    for sec in iter_sections(song):
        for note in sec.get("sectionNotes", []):
            lanes = note_lanes(note)
            if not lanes:
                continue
            new = []
            for lane in lanes:
                if 0 <= lane < keys:
                    new.append(lane + keys)
                elif keys <= lane < 2 * keys:
                    new.append(lane - keys)
                else:
                    new.append(lane)  # 越界（事件音符）原样保留
            note[1] = new
            n += 1
    log(f"玩家↔对手互换完成：{keys}K/侧，{n} 个音符的两侧归属已对调"
        f"（小节标记不变，BF 音符号对手、对手音符归 BF）")
    return {"swapped": n, "keys": keys}


def swap_sides_file(chart_path: str, out_path: str, log=print) -> Dict[str, Any]:
    """读 chart → 互换两侧 → 写回 JSON（保留原始外壳结构）。"""
    with open(chart_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    song = load_chart_data(data)
    res = swap_sides(song, log)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log("已写出 -> " + out_path)
    return res


# ---------------------------------------------------------------- 合并玩家轨道（一人唱两）

def merge_player_tracks(song: Dict[str, Any], target: str = "player",
                        conflict: str = "other_side", tolerance: float = 10.0,
                        log=print) -> Dict[str, Any]:
    """
    合并玩家轨道 —— 把「对手侧」的音符并到「玩家侧」，做成一人唱两（一个人打两段）。

    为什么需要（FNF 官方谱面语义，已核对 FNF Wiki / 官方 Chart Editor 手册）：
      · noteData 0..keys-1 与 keys..2*keys-1 分属两侧；mustHitSection=true 时
        低轨是玩家(BF)，false 时高轨是玩家。
      · 从别的引擎拿来的谱子常是「玩家一段、对手一段」对唱；想让一个玩家唱两段
        就得把对手音符搬到玩家侧。
      · 但合唱段双方同一时刻同一方向都有音符，硬搬会造成同一轨同一时刻两个音符
        → 打架 / 互扰（部分引擎会吞掉一个 note，甚至判定异常）。

    冲突处理（conflict）：
      "other_side" 冲突音符让到另一边（对手侧），合唱时各打各的、互不干扰 ★推荐
      "nearest"    冲突音符挪到玩家侧最近的空闲轨道（全部并进玩家侧，会换方向）
      "drop"       丢弃冲突音符（最保守，不推荐）

    target:
      "player"   合并到玩家侧（默认，一人唱两）
      "opponent" 反过来合并到对手侧（把正常谱反转成对手独唱）

    tolerance: 判定「同一时刻」的时间窗(ms)，默认 10ms。
    返回统计 dict。保留小节结构 / mustHitSection / 长按 / noteType / 和弦。
    """
    layout = detect_layout(song)
    keys = layout["keys"]
    for wn in layout["warnings"]:
        log("⚠ " + wn)
    tol = max(0.0, float(tolerance))
    to_opp_side = (target == "opponent")

    # 目标侧 = 我们要把音符并进去的那一侧；另一边 = 冲突时的去处
    def lane_of(rel: int, must_hit: bool, other: bool = False) -> int:
        """rel(0..keys-1) → 绝对轨道。other=True 表示放到「另一边」。"""
        # must_hit=true: 玩家在低轨；false: 玩家在高轨
        player_is_low = bool(must_hit)
        want_player = (not to_opp_side) ^ bool(other)  # XOR: other 时翻到对面
        if player_is_low:
            return rel if want_player else rel + keys
        return rel + keys if want_player else rel

    merged = conflicts = stays = 0

    for sec in iter_sections(song):
        must_hit = bool(sec.get("mustHitSection", False))
        notes = sec.get("sectionNotes", [])
        if not notes:
            continue

        # 第一遍：目标侧原有的音符先占位（保证合唱时玩家自己的音符不被挤走）
        occupied = {}   # t_key -> set(rel)
        for n in notes:
            lanes = note_lanes(n)
            if not lanes:
                continue
            t = float(n[0])
            tk = round(t / tol) if tol > 0 else t
            for lane in lanes:
                if lane < 0 or lane >= 2 * keys:
                    continue
                is_bf = note_belongs_to_bf(must_hit, lane, keys)
                on_target = (is_bf and not to_opp_side) or ((not is_bf) and to_opp_side)
                if on_target:
                    rel = lane - keys if lane >= keys else lane
                    occupied.setdefault(tk, set()).add(rel)

        # 第二遍：把「另一边」的音符并进目标侧，冲突的按策略处理
        for n in notes:
            lanes = note_lanes(n)
            if not lanes:
                continue
            t = float(n[0])
            tk = round(t / tol) if tol > 0 else t
            new_lanes = []
            for lane in lanes:
                if lane < 0 or lane >= 2 * keys:
                    new_lanes.append(lane)      # 事件/越界音符原样保留
                    continue
                is_bf = note_belongs_to_bf(must_hit, lane, keys)
                on_target = (is_bf and not to_opp_side) or ((not is_bf) and to_opp_side)
                rel = lane - keys if lane >= keys else lane
                if on_target:
                    new_lanes.append(lane_of(rel, must_hit))
                    stays += 1
                    continue
                # 这是要合并过来的音符
                if rel not in occupied.setdefault(tk, set()):
                    occupied[tk].add(rel)
                    new_lanes.append(lane_of(rel, must_hit))
                    merged += 1
                else:
                    conflicts += 1
                    if conflict == "drop":
                        continue
                    if conflict == "nearest":
                        # 找目标侧同一时刻最近的空闲方向
                        for d in range(keys):
                            cand = (rel + d) % keys
                            if cand not in occupied[tk]:
                                occupied[tk].add(cand)
                                new_lanes.append(lane_of(cand, must_hit))
                                break
                        else:
                            new_lanes.append(lane_of(rel, must_hit, other=True))
                    else:   # other_side
                        new_lanes.append(lane_of(rel, must_hit, other=True))
            n[1] = new_lanes

    side_cn = "玩家侧" if not to_opp_side else "对手侧"
    log(f"合并完成：{keys}K/侧 → 全部并到{side_cn}（一人唱两）；"
        f"合并 {merged} 个、保留原位 {stays} 个、合唱冲突 {conflicts} 个"
        f"（策略：{ {'other_side':'让到另一边，互不干扰', 'nearest':'挪到最近空轨', 'drop':'丢弃'}.get(conflict) }）")
    return {"merged": merged, "conflicts": conflicts, "stays": stays,
            "keys": keys, "target": target, "conflict": conflict}


def merge_player_tracks_file(chart_path: str, out_path: str,
                             target: str = "player",
                             conflict: str = "other_side",
                             tolerance: float = 10.0, reverse: bool = False,
                             log=print) -> Dict[str, Any]:
    """读 chart → 合并玩家轨道 → 写回 JSON（保留原始外壳结构，不覆盖源文件）。

    reverse=True：先按官方 "Swap Section" 语义把两侧整体互换，再合并
                  （即「把正常对唱谱反过来干成一人唱两」）。
    """
    with open(chart_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    song = load_chart_data(data)
    if reverse:
        swap_sides(song, log=log)
        log("已先整体反转两侧（Swap Section）")
    # drop 策略可能留下空轨道的音符，清掉避免写出脏数据
    for sec in iter_sections(song):
        sn = sec.get("sectionNotes", [])
        if sn:
            sec["sectionNotes"] = [n for n in sn
                                   if not (len(n) >= 2 and isinstance(n[1], list)
                                           and not n[1])]
    res = merge_player_tracks(song, target=target, conflict=conflict,
                              tolerance=tolerance, log=log)
    for sec in iter_sections(song):
        sn = sec.get("sectionNotes", [])
        if sn:
            sec["sectionNotes"] = [n for n in sn
                                   if not (len(n) >= 2 and isinstance(n[1], list)
                                           and not n[1])]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log("已写出 -> " + out_path)
    return res


# ---------------------------------------------------------------- osu!mania → FNF

def _osu_parse(path: str) -> Dict[str, Any]:
    """解析 .osu（mania）。返回结构化谱面信息。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        lines = f.read().splitlines()

    section = ""
    general: Dict[str, str] = {}
    meta: Dict[str, str] = {}
    diff: Dict[str, str] = {}
    timing_raw: List[List[str]] = []
    objects_raw: List[List[str]] = []

    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("//"):
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
            continue
        if section == "General":
            if ":" in s:
                k, v = s.split(":", 1)
                general[k.strip()] = v.strip()
        elif section == "Metadata":
            if ":" in s:
                k, v = s.split(":", 1)
                meta[k.strip()] = v.strip()
        elif section == "Difficulty":
            if ":" in s:
                k, v = s.split(":", 1)
                diff[k.strip()] = v.strip()
        elif section == "TimingPoints":
            timing_raw.append(s.split(","))
        elif section == "HitObjects":
            objects_raw.append(s.split(","))

    mode = int(general.get("Mode", "3") or 3)
    if mode != 3:
        raise ValueError(
            f"这个 .osu 是 Mode: {mode}（osu!mania 是 3），不是 mania 谱面，无法转换。\n"
            "请选择 osu!mania 4K/7K 等谱面的 .osu 文件。")

    keys = int(float(diff.get("CircleSize", "4") or 4))
    if keys < 1 or keys > 18:
        raise ValueError(f"CircleSize={keys} 超出合理键数范围")

    # TimingPoints：time,beatLength,meter,sampleSet,sampleIndex,volume,uninherited,effects
    bpm_points: List[Dict[str, float]] = []
    sv_count = 0
    for tp in timing_raw:
        if len(tp) < 2:
            continue
        try:
            t = float(tp[0])
            bl = float(tp[1])
        except ValueError:
            continue
        uninherited = 1
        if len(tp) >= 7:
            try:
                uninherited = int(tp[6])
            except ValueError:
                uninherited = 1
        if uninherited == 1 and bl > 0:
            bpm_points.append({"time": t, "bpm": 60000.0 / bl})
        else:
            sv_count += 1
    if not bpm_points:
        raise ValueError(".osu 里没有有效的 TimingPoints（找不到 BPM 信息）")
    bpm_points.sort(key=lambda p: p["time"])
    base = bpm_points[0]

    # HitObjects: x,y,time,type,hitSound,objectParams...
    notes: List[Dict[str, float]] = []
    skipped_slider = 0
    for ho in objects_raw:
        if len(ho) < 4:
            continue
        try:
            x = float(ho[0])
            t = float(ho[2])
            typ = int(ho[3])
        except ValueError:
            continue
        if typ & 2:      # slider：mania 谱基本没有，跳过并计数
            skipped_slider += 1
            continue
        col = min(keys - 1, max(0, int(x * keys / 512.0)))
        length = 0.0
        if typ & 128:    # mania hold note：参数为 endTime:...
            params = ho[5] if len(ho) > 5 else ""
            head = params.split(":")[0].strip()
            try:
                end_t = float(head)
                length = max(0.0, end_t - t)
            except ValueError:
                length = 0.0
        notes.append({"time": t, "lane": col, "length": length})
    if not notes:
        raise ValueError(".osu 的 [HitObjects] 里没有可用的音符")
    notes.sort(key=lambda n: (n["time"], n["lane"]))

    lead_in = 0
    try:
        lead_in = int(float(general.get("AudioLeadIn", "0") or 0))
    except ValueError:
        lead_in = 0

    return {
        "title": meta.get("Title", "") or os.path.splitext(os.path.basename(path))[0],
        "artist": meta.get("Artist", ""),
        "version": meta.get("Version", ""),
        "keys": keys,
        "bpm": round(base["bpm"], 3),
        "bpm_points": bpm_points,
        "notes": notes,
        "audio": general.get("AudioFilename", ""),
        "lead_in": lead_in,
        "sv_ignored": sv_count,
        "skipped_slider": skipped_slider,
    }


def osu2fnf(osu_path: str, out_path: str, offset_ms: float = 0.0,
            speed: float = 2.4, log=print) -> Dict[str, Any]:
    """
    osu!mania 谱面 → FNF chart JSON。
    offset_ms: 全局时间偏移（正=延后）。osu 的 AudioLeadIn 会让开头空出一段，
               想抵消就填负值（如 -800）。
    输出玩家独奏谱（mustHitSection 全 true，音符写 lane 0..keys-1）；
    keys>4 时写 mania=keys-1 + keyCount。
    """
    m = _osu_parse(osu_path)
    keys = m["keys"]
    if m["sv_ignored"]:
        log(f"⚠ 忽略 {m['sv_ignored']} 个 SV 变速点（FNF 不支持滚动变速）")
    if m["skipped_slider"]:
        log(f"⚠ 跳过 {m['skipped_slider']} 个 slider 物件（mania 罕用）")
    if m["lead_in"]:
        log(f"谱面 AudioLeadIn = {m['lead_in']}ms；如感觉开头对不上，"
            f"把偏移设为 {-m['lead_in']} 试试")

    if len(m["bpm_points"]) > 1:
        log(f"⚠ 检测到 {len(m['bpm_points'])} 个变速点，已取第一个"
            f"（{m['bpm']} BPM）作为全曲 BPM，变速未保留")

    beat_ms = 60000.0 / m["bpm"]
    section_ms = beat_ms * 4.0        # 一小节 = 4 拍 = 16 步
    sections: List[Dict[str, Any]] = []
    for n in m["notes"]:
        idx = int((n["time"] + offset_ms) // section_ms)
        while len(sections) <= idx:
            sections.append({"sectionNotes": [], "mustHitSection": True,
                             "lengthInSteps": 16})
        t = n["time"] + offset_ms
        sections[idx]["sectionNotes"].append(
            [round(t, 3), [n["lane"]], round(n["length"], 3)])
    # 丢掉全空小节
    sections = [s for s in sections if s["sectionNotes"]]
    if not sections:
        raise ValueError("偏移后没有任何音符落入小节")

    song: Dict[str, Any] = {
        "song": m["title"] or "unknown",
        "player1": "bf",
        "player2": "dad",
        "bpm": m["bpm"],
        "speed": speed,
        "needsVoices": False,
        "notes": sections,
        "generatedBy": "fosu",
    }
    if keys > 4:
        song["mania"] = keys - 1      # FNF 语法：keys = mania + 1
        song["keyCount"] = keys

    holds = sum(1 for s in sections for n in s["sectionNotes"] if n[2] > 0)
    total = sum(len(s["sectionNotes"]) for s in sections)
    log(f"解析完成：{keys}K，BPM {m['bpm']:g}，音符 {total} 个（长按 {holds}），"
        f"{len(sections)} 个小节")

    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"song": song}, f, indent=2, ensure_ascii=False)
    log("FNF 谱面已生成 -> " + out_path)
    return {"keys": keys, "bpm": m["bpm"], "notes": total, "holds": holds,
            "sections": len(sections), "audio": m["audio"],
            "lead_in": m["lead_in"], "out": out_path}


# ---------------------------------------------------------------- 音频 → OGG

def convert_audio_to_ogg(src: str, out_dir: str, name: str = "Inst.ogg",
                         volume: float = 1.0, ffmpeg: Optional[str] = None,
                         log=print) -> str:
    """把任意音频/视频音轨转成 FNF 用的 OGG（VBR q5），输出到 out_dir/name。"""
    if not os.path.isfile(src):
        raise FileNotFoundError(f"找不到音频文件：{src}")
    if ffmpeg is None:
        ffmpeg = find_ffmpeg()
    if not _has_audio_stream(src, ffmpeg):
        raise FileNotFoundError(
            f"{os.path.basename(src)} 里没有音轨，没法转成 OGG。")
    out_path = os.path.join(out_dir, name)
    log(f"转码 OGG：{os.path.basename(src)} -> {name}")
    transcode_audio(src, out_path, volume, ffmpeg=ffmpeg, log=log)
    log("OGG 完成 -> " + out_path)
    return out_path
