# fosu — FNF (PsychEngine) → osu!mania 一键转谱器

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
