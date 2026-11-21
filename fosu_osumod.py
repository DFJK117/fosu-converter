# -*- coding: utf-8 -*-
"""
fosu_osumod - OSU 模组包（.osz / .olz / .osu / .zip / 解压目录）-> 完整 FNF 模组

产物可直接丢进 Psych Engine 的 mods/<名>/ ：
  <Mod>/_polymod_meta.json
  <Mod>/data/<song>/<song>-<diff>.json     每个 .osu 一个难度
  <Mod>/data/<song>/events.json
  <Mod>/songs/<song>/Inst.ogg              从 osu 音频转码（ffmpeg）
  <Mod>/songs/<song>/Voices.ogg            静音占位（可选）
  <Mod>/weeks/<song>.json                  让它出现在 Freeplay
  （封面/图标不生成 —— 用引擎默认 bf/face）

依赖 fosu_core 的 ffmpeg 定位 / 转码 / _osu_parse。
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from typing import Any, Dict, List, Optional

import fosu_core as fc

OSU_AUDIO_EXT = ('.ogg', '.mp3', '.wav', '.m4a', '.flac', '.opus')
OSU_IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.bmp')


def _zip_extract_safe(zip_path: str, dest: str, log=print) -> int:
    """解 zip：兼容 cp437/utf-8 文件名、拦 zip slip、跳过坏项。返回解出的文件数。"""
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if not (info.flag_bits & 0x800):        # 无 UTF-8 标志 -> 试 cp437->utf-8
                try:
                    name = name.encode('cp437').decode('utf-8')
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            name = name.replace('\\', '/')
            parts = [p for p in name.split('/') if p not in ('', '.')]
            if not parts or '..' in parts:
                continue
            target = os.path.join(dest, *parts)
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                with z.open(info) as src, open(target, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                n += 1
            except Exception as ex:
                log("跳过解包失败项 %s：%s" % (name, ex))
    return n


def _osu_meta_quick(path: str) -> Dict[str, Any]:
    """快速读 .osu 的 General/Metadata/Difficulty（读到 HitObjects 就停）。"""
    out = {"title": "", "artist": "", "version": "", "keys": 4, "audio": "", "mode": 3}
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            sec = ""
            for ln in f:
                s = ln.strip()
                if not s or s.startswith("//"):
                    continue
                if s.startswith("[") and s.endswith("]"):
                    sec = s[1:-1]
                    if sec == "HitObjects":
                        break
                    continue
                if ":" not in s:
                    continue
                k, v = s.split(":", 1)
                k, v = k.strip(), v.strip()
                if sec == "General":
                    if k == "AudioFilename":
                        out["audio"] = v
                    elif k == "Mode":
                        try:
                            out["mode"] = int(v or 3)
                        except ValueError:
                            pass
                elif sec == "Metadata":
                    if k in ("Title", "Artist", "Version"):
                        out[k.lower()] = v
                elif sec == "Difficulty" and k == "CircleSize":
                    try:
                        out["keys"] = int(float(v or 4))
                    except ValueError:
                        pass
    except Exception:
        pass
    return out


def osu_pack_scan(pack_path: str, log=print) -> Dict[str, Any]:
    """扫描 .osz/.olz/.osu/.zip/目录。
    返回 {root, temp, charts:[{path,title,artist,version,keys,audio}], audio, images}
    temp=True 表示 root 是临时解包目录（调用方负责清理）。"""
    if not os.path.exists(pack_path):
        raise FileNotFoundError("找不到文件：" + pack_path)
    low = pack_path.lower()
    temp = False
    only = None                     # 单 .osu 输入：只处理这一个文件
    if os.path.isdir(pack_path):
        root = pack_path
    elif low.endswith(('.osz', '.olz', '.zip')) or zipfile.is_zipfile(pack_path):
        root = tempfile.mkdtemp(prefix="fosu_pack_")
        cnt = _zip_extract_safe(pack_path, root, log=log)
        log("解包：%d 个文件" % cnt)
        temp = True
    elif low.endswith('.osu'):
        root = os.path.dirname(os.path.abspath(pack_path))
        only = os.path.abspath(pack_path)
    else:
        raise ValueError("不认识的文件类型：需要 .osz / .olz / .osu / .zip 或解压后的目录")

    charts, audio, images = [], [], []
    for dp, dn, fn in os.walk(root):
        for f in fn:
            fl = f.lower()
            p = os.path.join(dp, f)
            if fl.endswith('.osu'):
                if only is not None and os.path.abspath(p) != only:
                    continue
                m = _osu_meta_quick(p)
                charts.append(dict(path=p, **m))
            elif fl.endswith(OSU_AUDIO_EXT):
                audio.append(p)
            elif fl.endswith(OSU_IMAGE_EXT):
                images.append(p)
    if not charts:
        if temp:
            shutil.rmtree(root, ignore_errors=True)
        raise ValueError("包里没有 .osu 谱面文件")
    charts.sort(key=lambda c: (c["keys"], c["version"]))
    audio.sort(key=lambda p: os.path.getsize(p), reverse=True)
    images.sort(key=lambda p: os.path.getsize(p), reverse=True)
    return dict(root=root, temp=temp, charts=charts, audio=audio, images=images)


# 引擎 Paths.formatToSongPath 用的两套字符规则（见 _fnf_song_slug 注释）
_INVALID_PATH_CHARS = re.compile(r'[~&\\;:<>#]')
_HIDE_PATH_CHARS = re.compile(r"[.,'\"%?!]")


def _fnf_song_slug(title: str) -> str:
    """Psych 歌曲目录名规范 —— 必须与引擎 Paths.formatToSongPath 逐字符一致。
    引擎源码（Paths.hx:763）：
      path.replace(' ', '-')
      非法字符 [~&\\;:<>#] -> '-'
      隐藏字符 [.,'"%?!]  -> 删除
      最后 toLowerCase
    旧版用 safe_name 自创规则（括号/引号处理不同），
    歌名带括号或标点时目录名与引擎不一致 -> 引擎找不到谱面也找不到音频。"""
    s = (title or "song")
    s = s.replace(' ', '-')
    s = _INVALID_PATH_CHARS.sub('-', s)
    s = _HIDE_PATH_CHARS.sub('', s)
    s = s.lower()
    return s or 'song'


def _diff_slug(version: str, fallback: str = "hard") -> str:
    """osu 难度名 -> FNF 难度 key（显示名，文件后缀用 formatToSongPath 再转一次）。"""
    v = (version or "").lower()
    if "beginner" in v or "easy" in v:
        return "easy"
    if "normal" in v:
        return "normal"
    if "hard" in v:
        return "hard"
    for name in ("insane", "extra", "expert", "lunatic", "master", "hyper", "another"):
        if name in v:
            return name
    out = ''.join(ch if (ch.isascii() and ch.isalnum()) else '-' for ch in v)
    while '--' in out:
        out = out.replace('--', '-')
    out = out.strip('-')
    return out or fallback




def _fmt_path(s: str) -> str:
    """引擎 formatToSongPath 的完整等价实现（文件名/目录名/难度后缀都用它）。"""
    return _fnf_song_slug(s)


def _osu_model_to_fnf(model: Dict[str, Any], offset_ms: float,
                      speed: float, song_title: str = "") -> Dict[str, Any]:
    """_osu_parse 结果 -> FNF song dict（玩家独奏，mustHitSection 全 true）。
    song_title 非空时强制用它 —— 引擎拿 json 里的 song 字段拼音频路径
    songs/<fmt(song)>/Inst，多难度包里各 .osu 的 Title 万一不一致，
    不强制统一就会出现「谱面在 A 目录、音频去 B 目录找」。"""
    bpm = model["bpm"]
    section_ms = (60000.0 / bpm) * 4.0
    keys = model["keys"]
    sections: List[Dict[str, Any]] = []
    last = -1
    for n in model["notes"]:
        t = n["time"] + offset_ms
        if t < 0:
            continue
        idx = int(t // section_ms)
        while last < idx:
            sections.append({"sectionNotes": [], "mustHitSection": True,
                             "lengthInSteps": 16})
            last += 1
        sections[idx]["sectionNotes"].append(
            [round(t, 3), int(n["lane"]), round(n["length"], 3)])
    song = {
        "song": song_title or model["title"] or "unknown",
        # 默认角色/舞台（用户指定）：玩家 BF、对手 DAD、GF 默认、舞台默认 stage
        "player1": "bf",
        "player2": "dad",
        "gfVersion": "gf",
        "player3": "gf",
        "stage": "stage",
        "bpm": bpm, "speed": speed, "needsVoices": False,
        "notes": sections, "generatedBy": "fosu (osu pack -> fnf mod)",
    }
    if keys > 4:
        song["mania"] = keys - 1
        song["keyCount"] = keys
    return song


def _find_audio(audio_list: List[str], declared: str) -> Optional[str]:
    if declared:
        base = os.path.basename(declared).lower()
        for p in audio_list:
            if os.path.basename(p).lower() == base:
                return p
        stem = os.path.splitext(base)[0]
        for p in audio_list:
            if os.path.splitext(os.path.basename(p).lower())[0] == stem:
                return p
    return audio_list[0] if audio_list else None


def _make_silent_ogg(out_path: str, seconds: float = 0.2, log=print) -> bool:
    try:
        ffmpeg = fc.find_ffmpeg()
        cmd = [ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
               "-t", "%.2f" % seconds, "-c:a", "libvorbis", "-q:a", "5", out_path]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=fc._NO_WINDOW)
        return os.path.isfile(out_path)
    except Exception as ex:
        log("静音 Voices 生成失败（可忽略）：%s" % ex)
        return False


def _install_audio(audio_src: str, songs_dir: str, log=print) -> str:
    """把包内音频装成 songs/<song>/Inst.ogg。
    已是 OGG -> 直接复制（零转码，秒出、无损）；其他格式才用 ffmpeg 转。"""
    dst = os.path.join(songs_dir, "Inst.ogg")
    ext = os.path.splitext(audio_src)[1].lower()
    if ext == '.ogg':
        if os.path.abspath(audio_src) != os.path.abspath(dst):
            shutil.copyfile(audio_src, dst)
        log("音频直接复用（包内已是 OGG，零转码）：%s（%.2f MB）"
            % (os.path.basename(audio_src), os.path.getsize(dst) / 1048576.0))
        return dst
    fc.convert_audio_to_ogg(audio_src, songs_dir, "Inst.ogg", log=log)
    return dst


def osu_pack_to_fnf_mod(pack_path: str, out_mod_dir: str = "", mod_name: str = "",
                        offset_ms: float = 0.0, speed: float = 2.4,
                        make_week: bool = True,
                        silent_voices: bool = True, mode: str = "full",
                        log=print) -> Dict[str, Any]:
    """OSU 模组包 -> FNF（两种模式）：
      mode="full"  导出全模组：data/ + songs/(Inst.ogg 等) + weeks/ + images/ + _polymod_meta.json
      mode="chart" 只导出谱面：仅 data/<歌>/<歌>-<难度>.json（无音乐/无 week/无封面）
    谱面角色默认：玩家 bf、对手 dad、GF gf、舞台 stage。
    返回统计 dict。"""
    full = (mode != "chart")
    scan = osu_pack_scan(pack_path, log=log)
    root = scan["root"]
    try:
        charts = scan["charts"]
        log("发现 %d 个难度：%s" % (
            len(charts), '、'.join((c["version"] or '未命名') for c in charts)))

        # 难度命名：一个 .osz = 一首歌，每个 .osu = 一个难度，按包内名字来
        # （4K Insane -> insane，Sava's Easy -> easy；绝不强行改叫 hard）
        used, names = set(), []
        for c in charts:
            nm = _diff_slug(c["version"])
            base, k = nm, 2
            while nm in used:
                nm = "%s%d" % (base, k)
                k += 1
            used.add(nm)
            names.append(nm)
        # 排序：easy / normal / hard 在前，其余按键数降序（Freeplay 列表好看）
        std = {"easy": 0, "normal": 1, "hard": 2}
        order = sorted(range(len(charts)),
                       key=lambda i: (std.get(names[i], 3), -charts[i]["keys"], i))
        log("难度映射（%d 个）：%s" % (len(names), '、'.join(
            "%s -> %s" % ((charts[i]["version"] or '未命名'), names[i]) for i in order)))

        first = _osu_meta_quick(charts[0]["path"])
        title = first["title"] or os.path.splitext(os.path.basename(pack_path))[0]
        artist = first["artist"] or "unknown"
        song_slug = _fnf_song_slug(title)
        if not out_mod_dir:
            out_mod_dir = os.path.join(os.getcwd(), fc.safe_name(mod_name or title))
        mod_root = out_mod_dir
        data_dir = os.path.join(mod_root, "data", song_slug)
        songs_dir = os.path.join(mod_root, "songs", song_slug)
        weeks_dir = os.path.join(mod_root, "weeks")
        os.makedirs(data_dir, exist_ok=True)
        if full:
            os.makedirs(songs_dir, exist_ok=True)
            if make_week:
                os.makedirs(weeks_dir, exist_ok=True)
        log("导出模式：%s" % ("全模组（谱面+音乐+week+封面+meta）" if full
                              else "仅谱面（只有 data/<歌>/*.json）"))

        # 音频 -> Inst.ogg（仅全模组；仅谱面模式不碰音频）
        audio_src = None
        if full:
            audio_src = _find_audio(scan["audio"], first["audio"])
            if not audio_src:
                # v5.2：包里没音频时，按谱面长度生成等长静音 Inst.ogg。
                # 引擎 Paths.inst 找不到 Inst 会直接起歌失败（表现为「无音乐」甚至卡死），
                # 给一个等长静音至少能进歌、能玩谱，音频另行替换。
                try:
                    _m0 = fc._osu_parse(charts[0]["path"])
                    _dur = max((n["time"] + n.get("length", 0))
                               for n in _m0["notes"]) / 1000.0 + 5.0
                except Exception:
                    _dur = 180.0
                log("⚠ 包里没有音频：生成 %.0f 秒静音 Inst.ogg 兜底"
                    "（能进歌能打谱，音频需自行替换 songs/%s/Inst.ogg）"
                    % (_dur, song_slug))
                _make_silent_ogg(os.path.join(songs_dir, "Inst.ogg"),
                                 seconds=_dur, log=log)
            else:
                _install_audio(audio_src, songs_dir, log=log)
            if silent_voices:
                _make_silent_ogg(os.path.join(songs_dir, "Voices.ogg"), log=log)

        # 谱面 json（按难度顺序输出）
        #
        # 引擎铁律（Difficulty.hx: defaultDifficulty='Normal'）：
        #   Highscore.formatSong(song, diff) = fmt(song) + (该难度名==Normal ? '' : '-难度名')
        #   Normal 难度读的永远是「无后缀」文件 data/<歌>/<歌>.json。
        #   旧版全都写成 <歌>-<难度>.json，玩家一进歌（默认难度=Normal）就
        #   报 Missing chart —— 引擎看不到谱面，曲子自然也不会有音乐。
        # 所以：名字是 normal 的难度写 <歌>.json；其余写 <歌>-<难度>.json；
        # 若整包没有 normal 难度，则把主难度额外复制一份为 <歌>.json，
        # 并在 week 的 difficulties 里补一个 Normal，保证默认难度一定有谱可读。
        total = 0
        diffs = []                      # 显示名（写进 week json 的顺序）
        written = {}                    # 显示名 -> 文件名
        primary_file = None
        for i in order:
            c = charts[i]
            model = fc._osu_parse(c["path"])
            song = _osu_model_to_fnf(model, offset_ms, speed, song_title=title)
            cnt = sum(len(s["sectionNotes"]) for s in song["notes"])
            total += cnt
            nm = names[i]
            fname = (song_slug + ".json") if _fmt_path(nm) == "normal" \
                else "%s-%s.json" % (song_slug, _fmt_path(nm))
            p = os.path.join(data_dir, fname)
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"song": song}, f, indent=2, ensure_ascii=False)
            if primary_file is None:
                primary_file = p
            dit = {"name": nm, "file": fname}
            written[nm] = fname
            diffs.append(nm)
            log("  难度 %-10s -> %-24s %dK BPM %g 音符 %d" % (
                nm, fname, model["keys"], model["bpm"], cnt))

        default_file = os.path.join(data_dir, song_slug + ".json")
        if not os.path.isfile(default_file) and primary_file:
            shutil.copyfile(primary_file, default_file)
            diffs.insert(0, "Normal")
            log("  默认难度补位：%s -> %s（引擎默认难度为 Normal，必须有无后缀谱）"
                % (os.path.basename(primary_file), os.path.basename(default_file)))

        if full:
            with open(os.path.join(data_dir, "events.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"song": {"song": title, "events": []}}, f, indent=2,
                          ensure_ascii=False)

        # 封面/图标：不生成 —— 用引擎默认（bf/face），用户点名瞎生成的
        # 图标不如默认的。freeplay 图标走 week json 的 "bf"。

        # week json -> Freeplay 可见（仅全模组）
        # 格式对照官方 weeks/qt.json：songs 元素 = [歌名, 图标名, [R,G,B]]，
        # 图标名用 "bf"（assets 自带 icon-bf.png）；作者写进 _polymod_meta.json。
        if full and make_week:
            week = {
                "storyName": title,
                "difficulties": ", ".join(diffs),
                "hideFreeplay": False,
                "weekBackground": "stage",
                "weekBefore": "",
                "startUnlocked": True,
                "hiddenUntilUnlocked": False,
                "weekCharacters": ["dad", "bf", "gf"],
                "freeplayColor": [146, 113, 253],
                "songs": [[title, "bf", [146, 113, 253]]],
                "hideStoryMode": False,
                "weekName": title,
            }
            with open(os.path.join(weeks_dir, song_slug + ".json"), "w",
                      encoding="utf-8") as f:
                json.dump(week, f, indent=2, ensure_ascii=False)
            log("  week：%s.json（difficulties = %s）" % (song_slug,
                                                        ", ".join(diffs)))

        if not full:
            log("仅谱面导出完成 -> " + data_dir)
            return {"mod_root": mod_root, "song": song_slug, "title": title,
                    "artist": artist, "difficulties": diffs, "notes": total,
                    "audio": None, "bg": False}
        meta = {
            "title": mod_name or title,
            "artist": artist,
            "description": "Converted from osu!mania pack by fosu (%d 难度 / %d 音符)"
                           % (len(diffs), total),
            "version": "1.0.0",
            "api_version": "1.0.0",
            "mod_version": "1.0.0",
        }
        with open(os.path.join(mod_root, "_polymod_meta.json"), "w",
                  encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        log("FNF 模组已生成 -> " + mod_root)
        return {"mod_root": mod_root, "song": song_slug, "title": title,
                "artist": artist, "difficulties": diffs, "notes": total,
                "audio": audio_src}
    finally:
        if scan.get("temp"):
            shutil.rmtree(root, ignore_errors=True)
