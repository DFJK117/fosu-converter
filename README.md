# fosu — FNF (PsychEngine) → osu!mania 一键转谱器

**⬇️ 直接下载：[fosu.exe](https://github.com/DFJK117/fosu-converter/releases/latest/download/fosu.exe)**（单文件，已内置 ffmpeg，下载后双击即用）

> 上面的链接永远指向最新版 Release，无需登录、无需解压仓库。

把 Friday Night Funkin' 的 PsychEngine 谱面 JSON 一键转成 osu!mania 谱面包（.osz）。
纯 Python 标准库实现，GUI 零第三方依赖；打包好的单文件 exe 已内置 ffmpeg。

## 它能做什么

- **读取 PsychEngine 谱面 JSON**：普通 4K 谱和 Shaggy 这类带 `mania` 字段的多键谱都支持，自动检测每侧键数（4K / 5K / 6K / 7K / 8K / 9K）。
  检测逻辑优先看谱面实际使用的轨道（最可靠），再参考 `mania` 字段；`mania` 字段是遗留垃圾值时会自动按实际轨道转换并在日志里提示（例如 Purple Power 写着 6 实际是 7K）。
- **按小节正确区分 BF / 对手音符**（与 theleername mania-converter 同规则）：
  - 玩家焦点小节（`mustHitSection`）BF 音符写在低轨道；对手焦点小节 BF 音符写在高轨道。
  - 「导出音符」下拉可选：BF（玩家，默认）/ 对手 / 全部。
- **扣血音符过滤（默认开启）**：`noteType = 3` 的扣血音符不会转成 osu 音符，避免出现不该按的音；可取消勾选保留。
- **自动混音**：自动识别谱面同目录的 `Inst.ogg` / `Voices.ogg`，用内置 ffmpeg 混音（等比混合，带防爆音限幅），可分别调音量。
- **假变速识别**：`changeBPM` 字段与音符实际时间不符时按真实网格处理。
- **长按（sustain）转 osu!mania 长条**。
- **一键输出 .osz**：双击即可导入 osu! stable / lazer。
- **🎁 OSU 模组包 → FNF 模组（反方向整包转换）**：直接吃 `.osz`（osu! stable 谱面包）/ `.olz`（osu!lazer 谱包）/ `.osu` 单谱面 / `.zip` / 已解压目录，
  一次生成可直接丢进 Psych Engine `mods/` 的**完整模组目录**：
  - `data/<歌>/<歌>-<难度>.json`：**一个 .osz 就是一首歌的完整模组，包里每个 `.osu` 各自成为一个难度**，难度名原样保留（easy / normal / hard / insane / extra / expert…，单难度包统一叫 hard）
  - `songs/<歌>/Inst.ogg`：包内音频直接复用——**已是 OGG 就零转码复制**（字节一致），mp3/wav 等才用内置 ffmpeg 转
  - `songs/<歌>/Voices.ogg`：静音占位（防个别引擎强制加载崩）
  - `weeks/<歌>.json`：让它出现在 Freeplay
  - `images/<歌>/bg.png` + `images/icons/icon-<歌>.png`：包内封面自动生成
  - `_polymod_meta.json`：Polymod 元数据
  多 K 谱面（4K/5K/6K/7K/8K/9K）自动写 `mania` + `keyCount`；`AudioLeadIn` / 偏移与滚动速度可调。
  **一键**：选完包自动扫描并开始转换（窗口里可关掉「选包后自动开始」）。

## 下载

到 [Releases](../../releases) 页面下载 `fosu.exe`（单文件，已内置 ffmpeg，无需安装 Python 环境）。

## 使用方法

1. 双击 `fosu.exe`（首次启动会慢几秒，属正常解压过程）。
2. ① 选谱面 JSON（例如 `purple-power-easy.json`）。
3. ② 确认 Inst / Voices 路径（已自动填好），需要时调音量。
4. ③ 确认键数（默认「自动」）、导出音符侧边、扣血音符开关、难度名、HP/OD、偏移等。
5. ④ 点「开始转换」，完成后把 `.osz` 双击导入 osu! 即可。

## 命令行自测

```
fosu.exe --selftest 谱面.json Inst.ogg Voices.ogg 输出.osz
```

## 命令行：OSU 包 → FNF 模组

```
fosu.exe --osu2mod <包.osz/.olz/.osu> [输出模组目录] [--offset ms] [--speed 2.4] [--no-week]
```

不给输出目录时默认在包旁边生成 `<包名>_FNFMod/`。

## 从源码运行 / 构建

运行（Python 3.8+，标准库即可）：

```
python fosu_app.py
```

打包单文件 exe（PyInstaller）：

```
pip install pyinstaller
# 下载 BtbN GPL 版 ffmpeg，解压到 packaging/ffmpeg-master-latest-win64-gpl/
cd packaging
pyinstaller fosu.spec
```

`packaging/fosu.spec` 会把 `ffmpeg.exe` 一起打进 exe，输出 `dist/fosu.exe`。

## 常见问题

- **Windows 弹 SmartScreen 蓝色提示**：点「更多信息」→「仍要运行」（未购买代码签名证书，属正常现象）。
- **杀软误报**：PyInstaller 单文件程序常见误报，加信任后使用即可；本工具只在你选择的文件夹内读写、调用内置 ffmpeg 处理音频。
- **只要谱面不要音频**：Inst 留空即可（会生成无音频谱面包）。
- **想转对手音符**：把「导出音符」改成「对手」；「全部」会把两侧音符合并到同一组轨道（osu 单人模式没有第二组轨道），双人合唱时两侧同刻同列的音符合并为一个。

## License

MIT

## 更新日志

### v5.1（2026-11-21）
- **修「又导入不了」**：主窗口拖拽入口改为万能导入——
  `.json` 谱面现在**重复拖入会替换**（旧版已载入过一首后，再拖新谱会被静默忽略）；
  `.osu` 拖入直接打开「osu!mania 谱面 → FNF」窗口并自动载入；
  `.osz` / `.olz` / `.zip` / 含 `.osu` 的文件夹拖入直接打开「OSU 包 → FNF 模组」窗口并自动扫描转换；
  音频按文件名归位（Inst/instrumental → Inst，Voices/vocals → Voices）。
  两个子窗口本身也支持拖拽；标题栏显示 `1.3 · v5.1 (20261121)` 便于核对版本。
- **修「无法导出」**：
  1) 没检测到 Inst/Voices/视频音轨时不再直接拦死，改为询问「继续导出无声 osz？」——谱面照常可用；
     核心 `convert_all` 同步支持无音频打包。
  2) ffmpeg 检测从 `os.path.isfile("ffmpeg")` 改为「实体文件 或 在 PATH 里」，不再误报「缺少 ffmpeg」。
  3) 换谱后输出 .osz 路径自动跟着换（旧版会沿用上一首的路径）。
- 作者名推断修正：`<mod>/data/<歌>.json` 不再把目录名 `data` 当作者，改用模组名。

### v5.2（2026-11-21，修「引擎检测不到谱面 / 无音乐」）
按 NF 引擎源码把落盘路径对齐到逐字符一致：
- **默认难度无后缀谱**：引擎 `Difficulty` 的 defaultDifficulty = `Normal`，`Highscore.formatSong()`
  = `formatToSongPath(歌名) + (难度==Normal ? '' : '-难度')`，`Song.getChart()` 读
  `data/<歌>/<歌><后缀>.json`。旧版把 Normal 也写成 `<歌>-normal.json` → 引擎按 `<歌>.json` 找不到，
  报 Missing chart、进不了歌、自然也没有音乐。现在：
  Normal 直接写 `<歌>.json`；整包没有 Normal 时，把主难度额外复制一份 `<歌>.json`
  并在 week 的 difficulties 里补 `Normal`，保证默认难度必有谱。
- **目录名与引擎完全一致**：换成 `Paths.formatToSongPath` 的等价实现
  （空格→`-`；`[~&\;:<>#]`→`-`；`[.,'"%?!]`删除；小写）。旧版自创规则让带括号/标点的歌名
  目录不一致（谱面和音频都找不到）。
- **谱内 `song` 字段强制统一为包标题**：引擎用 `Paths.inst(SONG.song)` 拼音频路径，
  多难度包各 `.osu` 的 Title 不一致时会去别的目录找音频（无音乐）。
- **包里没音频兜底**：按谱面末音符长度生成等长静音 `Inst.ogg`，至少能进歌能打谱，
  不会再「点了没反应」。
- week json 补 `freeplayColor` / `hiddenUntilUnlocked`，角色栏改为 `["dad","bf","gf"]`。
