# 更新记录

服务、AstrBot 插件和模型权重分别管理版本；服务端更新不会自动改变模型权重或已有插件安装包。

## 2026-10-30 · 语音条后补一句短话、同轮重复提交抑制

- `feiyu_speak` 增加可选 `caption` 参数：语音条单独发出，音频发送成功后再补一条短文本，用一句话把语气带出来。顺序由 `deliver()` 内串行 await 保证，不依赖宿主把文字和音频放在同一消息链里。新增配置项 `caption_enable`（默认 `true`）。
- 不填 `caption`、旧任务记录里没有该字段、或关掉 `caption_enable` 时，消息链与「肥鱼音频已生成。」提示保持原样，升级后不需要改任何配置。
- 同一轮对话、同一类型、同一参数只提交一单：大模型在同一轮里重复调用 `feiyu_speak` 时返回既有任务号，不再重复合成和重复发送。签名取自 `(轮次, origin, kind, payload)`，轮次即入站消息 id，所以改文字、改声线、换附件或换会话仍会正常提交；用户下一条消息里重复说同一句话也不受影响。
- 重复调用的判定会先占位再提交，两次相同调用并发进入时只有一次真正下发，另一次直接返回「正在提交」。
- 命中旧记录时会检查那一单的状态：仍在生成或已成功投递才判为重复；提交失败、投递失败或记录丢失时放行重新提交，不再回「音频会照常发出」。
- 工具返回与说明补充「一次调用就完成，不要重复调用」，减少同类重复调用。
- 陈旧记录的回收不再只看时间：当前轮次的记录一律保留，其他轮次里仍在生成的任务也保留，只有已经结束的才按 20 秒窗口清掉。此前生成超过 20 秒时，同一轮再调一次会被当成新请求重新下单。
- 新增 `tests/test_dedupe.py`，覆盖并发占位、提交中与生成中超过窗口、同轮已投递、跨轮放行、失败重试、异常回收与陈旧记录回收，可直接 `python tests/test_dedupe.py` 运行。
- 插件侧改动，声音服务、模型权重、HTTP 接口与命令别名不变。

## 2026-10-27 · Intel XPU（Arc / Core Ultra 核显）实验支持

- 以需显式选择的实验后端接收 PR #1，保留贡献者历史及有限实机记录；维护者尚无 Intel 实机或新增听感验收。集成时修复混合 Windows／WSL 唱歌的阶段路径、跨平台测试，补充四种宿主组合编排回归；保留已有 Colab 改动。
- 维护者回归：Windows 25 项后端、17 项服务、18 项乐谱、2 项 HTTP 合约、18 项 Colab 测试通过；Linux 19 项后端单元测试通过。真实 CUDA HTTP 上两声线、RVC 与 15 秒按谱唱歌均通过；两声线与既有同输入输出 SHA 相同。

- 增加 `xpu` 后端预设（`runtime_profiles/xpu.json`，torch / torchaudio 2.11.0+xpu），TTS、RVC、SVS 三条路线都能在 Intel GPU 上推理。XPU 在 Windows 和 Linux 上有原生 PyTorch wheel，所以这个预设**直接在宿主系统安装和运行**：Windows 运行环境 profile 增加 `host: windows`，服务端启动 Python 时不再包 `wsl.exe`，也不做 `/mnt/` 路径转换；Linux 直接调用本机 Python；`setup_runtime.py --backend xpu` 在 Windows 上用 `uv` 提供 Python 3.11。
- 适配方式：TTS 先在 CPU 加载再把 llm / flow / hift 与输入张量所用设备移到 `xpu:0`；RVC 显式设置 `xpu:0`，RMVPE 也在 XPU 上跑；SVS 用 FP32（上游 FP16 autocast 只支持 CUDA）。设备相关的代码集中到 `feiyu_runtime/common.py` 的 `accelerator()` / `device_type()` / `seed_all()`，CUDA 与 ROCm 路径保持原样。适配器启动前设置 `PYTORCH_ENABLE_XPU_FALLBACK=1`。
- Windows 原生运行时，torchaudio 2.9 以后的 `load` 会走需要共享 FFmpeg DLL 的 torchcodec，适配器改用 soundfile 读取参考音频（上游参数 `backend='soundfile'` 本来就是这个意思）。
- 修复 `requirements-tts.txt` 漏了 CosyVoice 数据处理用到的 `pyarrow`（缺它时 TTS 在载入模型的 hyperpyyaml 阶段就报 `No module named 'pyarrow'`）。
- 修复 Windows 上 Git 的 `core.autocrlf` 会把 `assets/singing/phone_set.json` 改成 CRLF、导致 SHA 校验失败的问题：`.gitattributes` 把它标为 `-text`。
- 新增 `configs/voice_service.xpu.example.json` 与 `configs/singing.xpu.example.json`；单元测试增加 XPU 后端、FP16 拒绝、原生 Windows 命令拼装等用例。
- Windows 在一台 Arc 130T 核显（Core Ultra 5 225H，驱动 32.0.101.8243）上验证过：TTS 稳态约 3–4× 实时（同机 CPU 约 6–8×），15 秒演示歌曲按谱合成加换声约 5 分钟。Linux 在 Arc A770M 上通过三种引擎诊断和 TTS／RVC 真实 HTTP 验证；Linux TTS 的 float64 F0 预测器固定在 CPU，避开 XPU 不支持的 weight_norm kernel。没有做与 CUDA 输出的听感对比，Linux 按谱唱歌音频仍待验证，数据见 `server/VALIDATION.json`。

## 2026-10-27 · Colab CUDA 选择与模块导入修复

- 两份 Notebook 安装表单增加 `auto / cu128 / cu130`，按 Colab 内核的 CUDA runtime 选择固定的 PyTorch 2.11.0 构建，同步 wheel 源、依赖约束与 profile；不改写共享的本机 CUDA 预设。
- 安装与推理入口自动定位 `publication/server/feiyu_runtime`，无需手动创建 symlink 或设置 PYTHONPATH。
- 更新辅助脚本 SHA 与使用说明，18 项回归测试通过，真实本地 cu128 RVC CLI 输出与既有结果一致；用户反馈手工调整后 Colab 可用，自动修复版本的完整 Colab GPU 验收仍待完成。
- 模型权重、参考音频、索引、推理算法及已有插件安装包保持不变。

## 2026-10-25 · 使用指南与素材署名

- 将项目首页与面向 DeepSeek／DSH／AstrBot agent 的《小肥鱼的声音指南》分别组织，统一使用 🐋 标识。
- 明确可爱 `cute`、温柔 `gentle` 两条 TTS 声线，默认可爱；说明默认偏好和按场景显式切换的方式。
- 增加 agent 复用或安装 MSST、编排“分离 → RVC 换声 → 混音”的整曲翻唱说明。
- 增加简单演唱 demo 路线：有文件生成／工具调用权限、且唱歌组件已配置的 agent，可自行制作《小星星》这类简单旋律的 MIDI、配歌词，再调用按谱合成接口。
- 补充原始音频来源：B 站 UP 主 [星落落_oi](https://space.bilibili.com/30584746/)；训练素材均已取得原 UP 主授权同意。来源和授权说明同步至 TTS、RVC 的模型仓库与配套 GitHub 文档。
- 本轮为说明文件与元数据更新，模型二进制、训练步数及调用协议保持不变。

## 声音服务 v0.2.0 · 2026-10-25

- 增加 NVIDIA CUDA 与 AMD WSL ROCm 两套可选推理环境预设、安装器和环境诊断；通过服务端 `runtime_profile` 选择环境。
- 适配 TTS 的 CPU ONNX 前端、RVC 的 CPU RMVPE 选项，以及 SVS 的设备／精度配置；主模型使用所选 GPU 后端。
- CUDA RVC 干净安装、实际换声，以及 TTS／RVC／按谱唱歌的真实 HTTP 回归通过。
- **AMD／ROCm 已提供适配与安装流程，尚无实体 A 卡测试；属于待实机验收的实验支持。** 具体显卡、驱动与 WSL 组合需单独检查，不宣称所有 A 卡均已验证。
- 保持 AstrBot HTTP 请求兼容，修复运行环境 Python 路径与跨平台源码换行校验问题。

## 初始发布 · 2026-10-25

- 发布肥鱼 RVC v1 至 Hugging Face 与 ModelScope，保留原选定模型和匹配索引。
- 发布 AstrBot 插件 v0.1.0 与独立声音服务，提供 TTS、音频换声、可选 MIDI／ABC 按谱唱歌、异步任务与鉴权下载。
- 既有肥鱼 TTS 模型提供可爱与温柔两条声线。
