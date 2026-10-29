# 🐋 小肥鱼的声音指南：给主人说话，也给主人唱歌

这份指南写给正在 DeepSeek／DSH、AstrBot 或其他 Bot 宿主中工作的小肥鱼，以及替它部署服务的 AI agent。你的任务是把声音服务接好，理解主人的声线偏好，并把生成的音频真正发回对话。

先读 [给主人提示](#owner-message)，再按 [请求类型](#route-task) 选择路线。[声线切换](#tts-style)、[Bot 接入](#connect-bot)、[MSST 整曲翻唱](#cover-song) 可按需阅读；具体环境命令集中在后面的 [部署附录](#install)。

<a id="owner-message"></a>

## 主人让你读项目、自己安装时，先告诉主人这些

在当前对话里主动发一次简短说明。可以按你的语气改写，**下面三项信息都要告诉主人**：

> 主人，我可以自己准备这套声音服务，先告诉你三件事：
>
> 1. **最好装在 WSL 环境里运行，推理更稳定。** 我会先检查电脑已有的环境，再按显卡选择 CUDA 或 ROCm；A 卡这边还需要核对具体兼容情况。
> 2. **说话有两条声线：一条偏可爱，一条偏温柔，默认是可爱。** 你想换默认声线，或者想让我根据场景切换，比如聊天时可爱、安慰你时温柔，告诉我就好。我会记住，发声时选对应的声线。
> 3. **你给我 MIDI 或 ABC 主旋律乐谱和歌词，我配置好唱歌组件后，就能生成肥鱼人声演唱。** 内部是先按谱合成歌声，再用 RVC 换成肥鱼音色。如果电脑上有 MSST 这类人声／伴奏分离工具，或者你让我装好它们，我还可以把一首歌分离、翻唱，再和伴奏重新混音。
>
> 如果你想先听个 demo，**《小星星》这样的简单歌曲，我也可以自己制作 MIDI 谱子、配上对应歌词，再唱给你听**。配置好唱歌组件后，你不必先准备乐谱，可以直接让我试唱一段。

这条消息用于让主人了解选择，不是要求主人读安装手册或逐步操作。已有安装授权就继续推进；硬件、路径和现有工具能自己查到的，先查。确实缺少歌曲、已对齐参考或偏好时，再提出具体问题。主人已说过的默认声线不必重复询问。

<a id="route-task"></a>

## 先判断主人交给你的是什么

| 主人的请求或材料 | 你该走的路线 | 调用 |
|---|---|---|
| “说一句话”“读给我听”＋文字 | CosyVoice3 TTS | `feiyu_speak` 或 `POST /v1/speech` |
| 清唱／干净语音 WAV、FLAC，想换成肥鱼声音 | RVC 音色转换 | `feiyu_convert` 或 `POST /v1/voice-conversion` |
| MIDI／ABC 主旋律＋歌词，想让肥鱼唱出来 | SoulX-Singer SVS → RVC | `feiyu_sing` 或 `POST /v1/singing` |
| 一首带伴奏的歌，想要肥鱼翻唱成品 | 本地分离 → RVC → 本地混音 | MSST 等本地工具＋换声接口＋FFmpeg 等混音工具 |

TTS 使用肥鱼 CosyVoice3 模型直接生成说话音频，当前输出 24 kHz。RVC 是 40 kHz 的 RVC v2 声库，使用 ContentVec、RMVPE 和匹配的 FAISS 索引。按谱接口自动串起 SVS 与 RVC：SVS 负责歌词、音高和时值，RVC 负责最终肥鱼音色。

已经唱好的原曲可以走分离翻唱路线，不需要主人再做 MIDI；有谱子、歌词且需要新的人声时，走按谱路线。先检查 `/v1/capabilities`，再检查实际样例；其中 `available` 表示配置可用，不代表音质或实体显卡已经验收。

### 主人没有乐谱时，你也可以主动做一个简单演唱 demo

如果你具备文件生成与工具调用权限，并已配置 SVS／RVC，可以先用《小星星》这类简单旋律演示：在本地用 `mido` 等工具写出单音 MIDI，配好对应的中文歌词，再上传调用 `/v1/singing`。这属于 DSH／agent 编写文件并调用声音工具的能力，具体取决于宿主权限；本仓提供的是后面的按谱合成接口。

按以下顺序完成，不必先让主人手工找谱：

1. 选择你能可靠写出旋律与节奏的简单曲目，先做短片段；《小星星》可作为示例。
2. 生成单一主旋律轨的 MIDI，写清速度、音高和时值。用对应歌词或自拟的中文 demo 歌词，保持一字一音；延音按 `_` 标记，先检查词谱对齐。
3. 检查唱歌能力和参考配置，上传谱子并提交任务，等到成功后下载真实音频。
4. 把 demo 发回当前对话，并说明是你制作的谱面与歌词版本。只有 MIDI 文件、尚未生成音频时，报告当前进度，不宣称已经唱完。

复杂歌曲需要可靠谱面；不能确定旋律时再向主人索取乐谱或改走原曲分离翻唱路线。生成 MIDI 并配歌词由你完成，SVS 负责把这些音乐信息变成歌声，RVC 负责最后的肥鱼音色。

<a id="tts-style"></a>

## 记住主人的声线偏好，并在每次说话时落实

| 主人说的声线 | `style` | 用法 |
|---|---|---|
| 可爱 | `cute` | 初始默认；未表达其他偏好时使用 |
| 温柔 | `gentle` | 主人指定，或按已约定的场景切换 |

主人可以改变长期默认，也可以只改变这一句话。将长期偏好记录在宿主已有的记忆／配置中；“这次温柔一点”不应自动覆盖长期默认。主人没有明确偏好时，先用可爱声线，提供同一句话的两份试听即可。

- **AstrBot 命令**：插件配置中的 `style` 控制 `/宇智波白饭鱼说话` 的默认声线。
- **AstrBot 大模型工具**：每次显式调用 `feiyu_speak(text="……", style="gentle")` 或 `style="cute"`。当前工具省略 `style` 时默认 `cute`，不会自动继承插件中改过的默认值。
- **DSH／其他 Bot 的 HTTP 桥接**：把偏好映射到请求 JSON 的 `style`。当前 API 省略该字段时同样默认 `cute`，所以根据偏好切换时应显式传值。

声线切换使用现成的 checkpoint＋参考音频路由，不需要为切换声线重装环境、重训模型或修改包内校验文件。这两个选项是声音风格配置；需要其他情感控制能力时，应先确认当前引擎和接口实际提供的参数。

<a id="connect-bot"></a>

## 接到 DeepSeek／DSH、AstrBot 或其他 Bot

### DeepSeek／DSH：把意图变成工具调用，再把音频送回去

DeepSeek／DSH 中的聊天模型负责理解请求，本地声音服务负责推理。需要宿主提供可用的 HTTP、终端或自定义工具能力，纯文本回复本身不会调用显卡。

本仓现成的客户端插件是 AstrBot 插件。DSH 可用宿主已有的 HTTP／终端工具调用服务，或按所用 DSH 版本注册轻量工具桥接；原生播放器、工具注册和音频消息格式由该宿主决定，本仓尚未发布独立 DSH 播放器插件。参考项目里的 `voice_say`、`/tts`、`/last` 或端口配置不属于本项目的协议，接已有播放器时要核对并做相应桥接。

无论宿主如何，工具流程保持一致：

1. 从私有配置取得声音服务地址和 API Key，使用 Bearer 鉴权。
2. 说话时提交 `/v1/speech`；音频、谱面等材料先上传 `/v1/assets`，再提交对应任务。
3. HTTP 202 返回的是任务编号。轮询 `/v1/jobs/{id}`，等到 `success`，失败时读取错误而不是宣称已经生成。
4. 下载 `/v1/jobs/{id}/audio`；服务端磁盘路径不作为客户端附件路径使用。
5. 用宿主实际支持的音频附件、播放器或文件发送能力，把下载的 WAV 发回当前对话。发送后给出简短文字，便于主人知道内容；下载、发送失败时如实说明具体阶段。

说话请求示例：

```http
POST /v1/speech
Authorization: Bearer <从私有配置读取>
Content-Type: application/json

{"voice":"feiyu","text":"主人，今天辛苦了。","style":"gentle"}
```

其他任务使用同一个基础地址 `http://127.0.0.1:9880`。API Key 从服务的 `.runtime/voice-service/api_key.txt` 私下配置到客户端，不能放进公开提示词、Git 仓库或日志。

| 请求 | 关键字段 |
|---|---|
| `POST /v1/assets` | multipart 字段 `file`；返回资源 `id` |
| `POST /v1/voice-conversion` | `voice: "feiyu"`、`audio_id: 上传返回的id` |
| `POST /v1/singing` | `voice: "feiyu"`、`score_id: 上传返回的id`、`lyrics: 歌词` |
| `GET /v1/jobs/{id}` | 状态包括 `queued`、`running`、`success`、`failed`、`interrupted` |
| `GET /v1/jobs/{id}/audio` | 成功后的 WAV；下载也需要鉴权 |

API 接收资源 ID，不接收客户端任意路径或远程音频 URL。MP3、M4A 等先在本地解码成 WAV／FLAC。较长歌曲使用后台任务轮询；服务串行运行 GPU 推理，避免同时加载多个大模型。

### AstrBot：使用已提供的命令和工具

插件中配置 `base_url`、`api_key`、`voice=feiyu`、`style`。命令与三个大模型工具见 [项目首页](../README.md#astrbot-用户)。插件负责上传消息附件、后台轮询、下载音频并向原会话发送；宿主还需配置对应平台的音频发送能力。

`feiyu_convert(source="attachment")` 读取当前消息的 WAV／FLAC 附件；`feiyu_sing(lyrics="……")` 读取当前消息的 MIDI／ABC 附件。如果你已经用本地 MSST 生成了文件，直接通过 HTTP 上传处理结果，或由宿主实际可用的文件工具接续流程，不把磁盘路径填进 `source` 参数。

<a id="cover-song"></a>

## 给主人翻唱一整首歌：先检查本地 MSST

这里的目标是交付“肥鱼人声＋伴奏”的歌曲成品。当前服务提供 RVC 换声，MSST 分离与最后混音由你调用本地工具编排；没有现成的 `/v1/separate`、`/v1/mix` 或一键 MSST 安装接口。

### 本机已经有 MSST／其他分离工具

先定位工具目录、独立 Python、已下载的分离模型与配置，以及可用的 CLI、Python 或本地服务入口。优先复用现有可用安装，查看所安装版本的说明与 `--help`，用短片段验证实际能生成原唱人声和伴奏。

MSST-WebUI 的 CLI 和 Python 调用方式见 [官方推理文档](https://github.com/SUC-DriverOld/MSST-WebUI/blob/main/docs/inference.md)。其预设 CLI 的用法示意如下；在 **MSST 自己的环境和目录中** 执行，预设文件必须是真实存在、模型已下载的配置：

```bash
python scripts/preset_infer_cli.py \
  -p /path/to/verified-preset.json \
  -i /path/to/song-input -o /path/to/separated -f wav
```

### 本机没有，主人让你安装 MSST

按 [MSST-WebUI 官方项目](https://github.com/SUC-DriverOld/MSST-WebUI) 选取合适版本，单独安装环境和用于人声／伴奏分离的模型；也可复用其他能可靠产出这两条音轨的工具。安装软件之外还要检查分离模型、对应配置和实际入口，先验短片段再处理整首歌。已有“安装 MSST／补齐翻唱工具”的授权就继续完成这些步骤，不把安装工作重新交给主人。

MSST 与肥鱼的 TTS、RVC、SVS 环境分开，已有 Windows 分离工具也可以配合 WSL 推理使用。使用 A 卡时，分离工具的后端兼容性需要单独验证，不能仅凭肥鱼 ROCm 配置安装成功推定 MSST 也已可用。

### 分离 → 换声 → 混音 → 发回给主人

1. **保存原曲和分离结果**。确认哪条是主唱、哪条是伴奏，留意伴奏里残留原唱或高音被错误分离的情况。
2. **转换主唱**。把分离出的 WAV／FLAC 人声上传到 `/v1/assets`，提交 `/v1/voice-conversion`，等待并下载肥鱼人声。
3. **准备混音**。RVC 人声当前是 40 kHz 单声道；先核对伴奏采样率、时间起点和长度，用 FFmpeg 等工具重采样并对齐，保留原始文件。
4. **混回伴奏**。按实测人声／伴奏电平设置增益，检查尾部和峰值，避免削波。未被要求时保留原调，不为贴近某个音域数值自动整曲移调。
5. **交付与试听**。发回完整混音，需要时附清唱版本；明确哪些处理成功、哪里仍有分离残留或演唱问题。数值检查通过与主人试听满意分别记录。

完整歌曲可以由你完成这条工具链，不要求主人亲手分离或混音；执行依赖本机真实可用的工具与模型。当前公开仓库没有封装这条完整工具链，也没有声明任意分离模型在所有歌曲上都有效。

<a id="install"></a>

## 部署附录：为小肥鱼准备声音服务

以下命令以公开仓库的 `server/` 为工作目录；本地开发仓库可在项目根目录执行。所有 `models/`、`configs/` 路径均相对于该目录，不相对于配置文件所在目录。

服务端支持显式选择 CUDA 或 ROCm 推理环境，两者使用同一份肥鱼模型权重和 RVC 索引。AMD 路径目前没有实体显卡验证，应作为可安装、可诊断的实验配置交付。

## 1. 选择后端

先在 Windows PowerShell 获取 GPU 名称和系统信息：

```powershell
Get-CimInstance Win32_VideoController | Select-Object Name, PNPDeviceID
wsl --list --verbose
wsl -d Ubuntu-24.04 -- uname -a
```

| 检测结果 | 选择 |
|---|---|
| 有可用 NVIDIA 独显 | `cuda`，先在 WSL 确认 `nvidia-smi` 可用 |
| 只有 AMD，且该型号/驱动/WSL 组合在 AMD 官方支持范围内 | `rocm`，装对应 Windows WSL 驱动和 ROCm 运行时 |
| 只有 Intel Arc / Core Ultra 核显，且 `torch.xpu` 能看到该 GPU | `xpu`，在 Windows 或 Linux 原生运行（不经过 WSL），见下方 “Intel XPU” |
| 混合显卡 | 默认用已可用的 NVIDIA；用户要求 AMD 或 Intel 时明确选择对应后端 |
| AMD 未列入支持组合、型号未知或 GPU 在 WSL 不可见 | 说明缺少哪项兼容条件；不能宣称 GPU 服务已装好 |

ROCm 采用 PyTorch 的 `cuda:0` 设备名，实际后端用 `torch.version.hip` 判别。不要把代码中的所有 `cuda` 替换成 `hip`，不要在同一个虚拟环境混装两个后端。参考 [AMD 当前兼容矩阵](https://rocm.docs.amd.com/en/latest/compatibility/compatibility-matrix.html) 和 [PyTorch HIP 说明](https://docs.pytorch.org/docs/main/notes/hip.html)。

仓库提供固定的依赖预设：

| 预设 | RVC / TTS | 可选 SVS | 初始精度/前端 |
|---|---|---|---|
| `runtime_profiles/cuda.json` | torch/torchaudio 2.11.0+cu128 | 2.8.0+cu128 | RVC/TTS FP32，SVS FP16，ONNX CPU |
| `runtime_profiles/rocm.json` | torch/torchaudio 2.11.0+rocm7.2 | 2.11.0+rocm7.2 | FP32，RMVPE CPU，ONNX CPU |
| `runtime_profiles/xpu.json` | torch/torchaudio 2.11.0+xpu | 2.11.0+xpu | FP32，RMVPE XPU（可选 CPU），ONNX CPU |

ROCm 预设的 wheel 来自 PyTorch 官方 `rocm7.2` 索引；wheel 存在不等于每张 Radeon 都获支持。先核对目标硬件与所选版本的官方条件。需要另一 ROCm 版本时新增独立预设、明确版本并重新验证，不静默改动这些预设。AMD FP32 SVS 显存和速度尚无实测，显存不足时先缩短分段，任何 FP16 调整都应记录条件。

### Intel XPU（Arc / Core Ultra 核显，Windows / Linux 原生）

**实验性、需显式选择 `--backend xpu`。** 保留现有可用 CUDA／ROCm 环境，不自动切换或覆盖；先做独立环境诊断、实际音频验证及试听。Intel 实机数据来自贡献者，维护者尚未独立复验。

Intel GPU 目前在 WSL2 里通常不可用；PyTorch 官方 XPU wheel 提供原生 Windows 和 Linux 版本，所以 `xpu` 预设应直接在宿主系统安装和运行。Windows 的 `runtime.local.json` 里 `host` 为 `windows`，服务端会直接启动该 Python，不再包一层 `wsl.exe`，路径也不做 `/mnt/` 转换；Linux 本来就直接调用本机 Python。前置条件：最新 Intel 显卡驱动、`git`、`ffmpeg`、[`uv`](https://docs.astral.sh/uv/) 和 Python 3.11。

原生 Linux 还需配置 Level Zero 和 OpenCL 驱动；只安装 PyTorch wheel 不够。Ubuntu 可安装下列软件包，`clinfo` 必须显示至少一个平台，否则 FFT 等算子会报 `OpenCL error -1`：

```bash
sudo apt-get update
sudo apt-get install -y libze-intel-gpu1 intel-opencl-icd clinfo  # 旧版 Ubuntu 的 Level Zero 包名可能是 intel-level-zero-gpu
clinfo
```

然后确认 PyTorch 能看到 GPU：

```powershell
# Windows PowerShell
uv venv probe --python 3.11
uv pip install --python probe\Scripts\python.exe --index-url https://download.pytorch.org/whl/xpu torch==2.11.0+xpu
probe\Scripts\python.exe -c "import torch; print(torch.xpu.is_available(), torch.xpu.get_device_name(0))"
```

```bash
# Linux
uv venv probe --python 3.11
uv pip install --python probe/bin/python --index-url https://download.pytorch.org/whl/xpu torch==2.11.0+xpu
probe/bin/python -c "import torch; print(torch.xpu.is_available(), torch.xpu.get_device_name(0))"
```

输出 `True` 和显卡名才继续。然后在仓库 `server/` 目录里为每个引擎各装一个环境：

```powershell
# Windows PowerShell
python scripts/setup_runtime.py --backend xpu --engine tts --destination C:\feiyu\xpu\tts
python scripts/setup_runtime.py --backend xpu --engine rvc --destination C:\feiyu\xpu\rvc
python scripts/setup_runtime.py --backend xpu --engine svs --destination C:\feiyu\xpu\svs   # 可选，按谱唱歌
```

```bash
# Linux
feiyu_python="$(uv python find 3.11)"
"$feiyu_python" scripts/setup_runtime.py --backend xpu --engine tts --python "$feiyu_python" --destination "$HOME/.local/share/feiyu-voice/xpu/tts"
"$feiyu_python" scripts/setup_runtime.py --backend xpu --engine rvc --python "$feiyu_python" --destination "$HOME/.local/share/feiyu-voice/xpu/rvc"
"$feiyu_python" scripts/setup_runtime.py --backend xpu --engine svs --python "$feiyu_python" --destination "$HOME/.local/share/feiyu-voice/xpu/svs"  # 可选，按谱唱歌
```

适配方式：XPU 不被上游 CosyVoice / Applio 识别，所以 TTS 先在 CPU 加载，再把 llm / flow / hift 与输入张量所用的设备移到 `xpu:0`；Linux 的 float64 F0 预测器固定在 CPU，避开 XPU 不支持的 weight_norm kernel；RVC 在 Applio 的 `Config` 中显式设置 `xpu:0`；SVS 使用 FP32（上游 FP16 autocast 只支持 CUDA）。适配器启动前设置 `PYTORCH_ENABLE_XPU_FALLBACK=1`，该设置不代表已验证任意缺失算子能自动回退；遇到不支持的算子仍可能失败，需要单独适配。核显与系统共享内存，速度明显低于独立显卡，请以实测为准（见 [VALIDATION.json](VALIDATION.json)）。Linux Arc A770M 已通过三种引擎诊断和 TTS／RVC 真实 HTTP 验证；Linux 按谱唱歌仍需额外权重与已对齐参考进行音频验收。

## 2. 安装独立推理环境

> 下面的通用命令以 WSL（CUDA / ROCm）为例；原生 Linux 安装 XPU 时使用上一节命令。

在 WSL Ubuntu 安装基础组件（Ubuntu 镜像中包名可能略有差异）：

```bash
sudo apt-get update
sudo apt-get install -y git ffmpeg libsndfile1 libsox-dev build-essential python3-dev
```

使用独立 Python 3.11。Ubuntu 24.04 默认 Python 不一定是 3.11，可以通过已安装的 uv 执行 `uv python install 3.11.17`，再用 `uv python find 3.11.17` 获取路径。不要替换系统 Python。以下命令都在 WSL 内、仓库 `server/` 目录执行：

将推理环境与上游源码放在 WSL 本地文件系统，例如 `$HOME/.local/share/feiyu-voice/`。避免把几套大型 Python 环境安装在 `/mnt/c/` 等 Windows 挂载盘上；本轮空目录安装测试实际观察到该位置的文件写入显著变慢。API 代码和模型包可以保留在 Windows 项目目录，通过 WSL 路径访问。

```bash
feiyu_python="$(uv python find 3.11.17)"
feiyu_backend=cuda  # AMD 改为 rocm
feiyu_env_root="$HOME/.local/share/feiyu-voice/$feiyu_backend"

"$feiyu_python" scripts/setup_runtime.py --backend "$feiyu_backend" --engine rvc \
  --python "$feiyu_python" --destination "$feiyu_env_root/rvc"
"$feiyu_python" scripts/setup_runtime.py --backend "$feiyu_backend" --engine tts \
  --python "$feiyu_python" --destination "$feiyu_env_root/tts"
```

只需要换声就只安装 RVC；只需要说话就只安装 TTS。安装器克隆固定 commit、复制 RVC 兼容源码、下载并校验通用 ContentVec/RMVPE 资产、安装选定后端、运行 `pip check`，输出依赖快照、源码 SHA 清单和诊断 JSON。每个引擎一个环境；现有目录会被拒绝，避免覆盖正在使用的安装。失败日志和目录保留排查，修复后用新的目标目录重试。

`--dry-run` 可先打印依赖安装计划。诊断包括实际 GPU 矩阵运算、卷积、FFT 和推理入口导入；它不会下载肥鱼权重，也不会替代实际音频验证。

请保留仓库 `.gitattributes`。RVC 兼容源码与模型包绑定的是原始字节 SHA，包括换行符；不要让编辑器或格式化器批量改写 `runtime_compat/Applio/`。安装器会先检查兼容源码 SHA，再复制到独立环境。

若复用现有兼容环境，可使用 `scripts/register_runtime.py --backend cuda --engine rvc --repo /path/to/Applio --python /path/to/env/bin/python --output /path/to/new/runtime.local.json`。这只读取环境、生成配置并运行诊断，不修改环境；版本和源码 commit 不匹配时应建立新环境。自建 Python 若需要额外共享库路径，在生成配置中填写 `python_library_dir`，并让安装/诊断命令使用同一个 `LD_LIBRARY_PATH`。

## 3. 下载模型、连接配置

下载以下仓库到服务端 `models/` 中，保持包内文件结构：

- TTS → `models/FeiyuVoice-CosyVoice3`：[Hugging Face](https://huggingface.co/Esmerubphire/FeiyuVoice-CosyVoice3) / [ModelScope](https://modelscope.cn/models/Esmerubphire/FeiyuVoice-CosyVoice3)。
- RVC → `models/FeiyuVoice-RVC`：[Hugging Face](https://huggingface.co/Esmerubphire/FeiyuVoice-RVC) / [ModelScope](https://modelscope.cn/models/Esmerubphire/FeiyuVoice-RVC)。

将安装器输出的两个 `runtime.local.json` 复制到服务端 `configs/runtime.rvc.cuda.local.json`、`configs/runtime.tts.cuda.local.json`；AMD 使用 `.rocm.local.json` 文件名。文件内容中的 Python 和源码路径必须是实际 WSL 路径，保留安装器生成的版本、commit、源码 SHA。若环境安装在 WSL ext4 中，复制到 Windows 项目时用 `/mnt/c/.../server/configs/` 目标路径。

把 `configs/voice_service.cuda.example.json`（或 `.rocm.example.json`）复制为 `configs/voice_service.json`。关键字段如下：

```json
{
  "engine": "cosyvoice3_release",
  "package": "models/FeiyuVoice-CosyVoice3",
  "runtime_profile": "configs/runtime.tts.rocm.local.json"
}
```

RVC 对应 `engine: feiyu_rvc_release` 和 RVC 的 profile。未安装的能力从服务配置中移除或置空对象。`runtime_profile` 指定时，采用本仓库的独立适配器，无需修改模型包的 `infer.py`、`run_rvc.py`、权重或包内校验文件，也不需要向包内写 `runtime.local.json`。没有该字段的旧配置继续使用包内启动器。

如果更改本地环境配置或源码，重新生成清单并重新验证，然后重载服务；不要手填虚假 SHA 来绕过校验。

## 4. 安装 API 与 AstrBot

API 是 CPU 环境，与三个推理环境分开。在 Windows 的 `server/` 目录：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-service.txt
.\scripts\Start-VoiceService.ps1 -Config .\configs\voice_service.json
```

也可以在 Linux/WSL 创建独立 `.venv` 并运行 `python -m feiyu_voice_service --config configs/voice_service.json`。服务在 WSL 内时，包体路径也必须是 Linux 可访问路径。

使用服务生成的本地 API Key 配置 AstrBot 插件，默认地址 `http://127.0.0.1:9880`。遵循现有服务说明的鉴权与监听地址设置。不要把 API Key、用户上传内容或机器路径提交到 GitHub。

`GET /v1/capabilities` 中 `available` 仍表示包体/配置可用；新增 `runtime_backend` 和 `runtime_verification` 表示选择的后端及配置检查层级。实际推理结果记录本次 GPU、后端和精度。现有上传、说话、换声、唱歌、任务查询和下载请求格式不变。

## 5. 可选 MIDI/ABC 唱歌

使用单音主旋律与中文歌词，一字一音，一字多音以 `_` 标记延音；ABC 的反复段先展开。多轨 MIDI 要选定主旋律轨，不能把整份伴奏或和弦直接当旋律。按谱接口的 `track`、`channel`、`bpm`、`transpose`、`pinyin_id` 等字段以 `/docs` 为准，保持主人给定的调性与节奏，按需显式设置。

额外安装 SVS：

```bash
"$feiyu_python" scripts/setup_runtime.py --backend "$feiyu_backend" --engine svs \
  --python "$feiyu_python" --destination "$feiyu_env_root/svs"
```

将 `configs/singing.cuda.example.json`（或 `.rocm.example.json`）复制为 `configs/singing.json`，下载指定 SVS 权重并提供自有已对齐 prompt WAV/JSON，填写其真实 SHA256。将 SVS profile 复制到 `configs/runtime.svs.cuda.local.json`（或 `.rocm.local.json`）；示例已设置 `svs_runtime_profile`，该字段优先于旧 `svs_runtime`。独立命令运行唱歌时，`rvc_runtime_profile` 指向选定 RVC profile；通过 API 调用时，服务会自动继承当前声音的 RVC profile。

开启 `voice_service.json` 的 `singing` 配置。SVS 与 RVC 分进程串行运行，不同时常驻显存。谱子和歌词解析在 CPU。精度来自 SVS profile，不用修改 HTTP 请求。用户未提供对齐参考时，只交付可用的 TTS/RVC，不把唱歌显示为可用。

## 6. 安装验收与故障处理

服务启动后可用 API 环境运行实际验收脚本（只验说话时省略 `--vocal`）：

```powershell
.venv\Scripts\python.exe scripts\validate_runtime_service.py --tts `
  --key-file .runtime\voice-service\api_key.txt --vocal C:\path\to\clean-vocal.wav `
  --output-dir .runtime\acceptance-new-install
```

可选唱歌再添加 `--score examples/singing/feiyu_demo.abc --lyrics "晨风轻轻吹过山岗云光落在我的身旁"`。输出目录须是新目录，验收结果不包含 API Key。

1. 保存各环境的 `diagnostic.json`；应为 success，后端与预期一致。GPU 不可用或装错 wheel 会报错，不自动退化成全 CPU 服务。
2. 通过原 HTTP 接口实际生成至少一条可爱 TTS、一条温柔 TTS和一条带索引的 RVC WAV；确认下载、时长、采样率、有限非静音，并试听。可选 SVS 再测试一份短谱子和连音/休止。
3. 记录 `worker_result.json` / 引擎 receipt；RVC 还应检查 `f0_devices`，确认 CPU 前处理是预设选择。不能把“GPU 名称可见”当作推理已在 GPU 上运行。
4. 相同权重跨 GPU 的 WAV 不要求逐字节一致。权重/索引身份必须一致，音质、音高和长音频稳定性要单独比较。
5. AMD 尚无实卡验收记录。完成某台机器测试后报告准确显卡、驱动、WSL、torch/ROCm 版本及通过项目，避免宣传“所有 A 卡兼容”。

常见问题：ONNX 报 NVIDIA 库缺失时检查是否误装 `onnxruntime-gpu` 或误用旧包内 TTS 启动器；新适配器明确使用 CPU ONNX。torch 被依赖替换时检查约束文件和 `pip check`，不要混装后端。缺失 `libpython` 时配置 Python 共享库路径；缺失 FFmpeg/音频库时补齐系统组件。AMD 算子问题先定位实际阶段，RMVPE 默认 CPU，主网络失败必须如实报错并保留日志，不自动换模型。
