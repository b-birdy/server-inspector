---
name: update-inspector-profile
description: 维护 server-inspector 项目的加密配置文件 profiles.enc。在用户要求"更新硬件数据库 / 添加新卡 / 调整对标系数 / 修改场景模板 / 更新模型列表"等涉及 profiles.json 内容变更时调用。负责解密→编辑→重新加密→验证→提交的完整闭环，确保仓库只暴露密文。
---

# 更新 server-inspector 加密配置流程

## 何时调用此 skill

用户提出任何针对 `server-inspector` 项目硬件数据库的变更请求时触发，例如：
- 「加一张新卡：XXX」/「补充某型号规格」
- 「调整 P800 对标系数为 0.55」
- 「更新 NVIDIA 基准数据」/「补充 H100 的某档位 benchmark」
- 「新增 2027 年发布的模型 XX」
- 「修改 single_node_8 场景的匹配规则」
- 「换一个加密密钥」

凡是动 `profiles.*` 文件内容的需求，都走这套流程。

## 项目关键信息

- 仓库本地路径：`C:\Users\Administrator\coding-project\server-inspector\`
- 远端测试机别名：`my-server`（SSH 已配置免密）
- 仓库远端：`git@github.com:b-birdy/server-inspector.git`
- 加密密钥（硬编码在 inspector.py，路径见下）：`svi-internal-2026-pkg-v1`
- 加密算法：`zlib 压缩 → 固定 key XOR → Base64`（对称，详见 `encode_profile_bytes` / `decode_profile_bytes`）

## 工作流

### 步骤 1 — 确认本地是否有明文 profiles.json

```bash
ls /c/Users/Administrator/server-inspector/profiles.json
```

- **存在**：直接进入步骤 2 编辑
- **不存在**（仓库刚 clone 或被清理过）：从 `profiles.enc` 解密恢复明文

解密恢复命令：

```bash
cd /c/Users/Administrator/server-inspector && python -c "
import sys
sys.path.insert(0, '.')
from inspector import decode_profile_bytes
from pathlib import Path
enc = Path('profiles.enc').read_bytes()
Path('profiles.json').write_bytes(decode_profile_bytes(enc))
print('Decoded profiles.json restored')
"
```

> 注意：`profiles.json` 在 `.gitignore` 中，**只是本地工作副本**，不会被提交。

### 步骤 2 — 修改 profiles.json

使用 Edit / Write 工具按用户需求修改 `profiles.json`。

**修改后必须用 Python 验证是合法 JSON**：

```bash
python -c "import json; json.load(open(r'C:\Users\Administrator\coding-project\server-inspector\profiles.json', encoding='utf-8')); print('JSON OK')"
```

JSON 格式错误必须立即修复，不要继续后续步骤。

### 步骤 3 — 重新生成 profiles.enc

```bash
cd /c/Users/Administrator/server-inspector && python inspector.py --encode-profile profiles.json
```

预期输出：`[OK] 已生成加密配置: profiles.enc (XXXX bytes)`

文件大小变化是正常的（取决于内容修改量）。

### 步骤 4 — 远端测试验证

**必须做**：把更新后的脚本和加密配置传到远端，跑一次完整流程，确保解密+解析+采集+报告生成全链路无错。

```bash
scp /c/Users/Administrator/server-inspector/inspector.py \
    /c/Users/Administrator/server-inspector/profiles.enc \
    my-server:~/server-inspector/

ssh my-server "cd ~/server-inspector && python3 inspector.py --output-dir ./reports 2>&1 | tail -5"
```

如果改动涉及报告渲染（场景模板、模型列表、对标系数等），把最新报告回传查看：

```bash
latest=$(ssh my-server "ls -t ~/server-inspector/reports/*.md | head -1")
scp my-server:"$latest" /c/Users/Administrator/server-inspector/reports/verify_latest.md
```

阅读 `verify_latest.md` 验证改动是否按预期生效（性能数字合理、模型列表正确、对标说明无误等）。

### 步骤 5 — Git 提交

**只提交 `profiles.enc`**（`profiles.json` 已在 .gitignore，git status 不会看到）：

```bash
cd /c/Users/Administrator/server-inspector
git add profiles.enc
# 如果 inspector.py 也改了，一起加
git status  # 确认没有 profiles.json
git commit -m "data: <描述这次配置变更>

具体改了什么、为什么改"
git push origin master
```

commit message 风格：
- 配置数据更新：`data: 添加 XX 卡规格`、`data: 调整 P800 对标系数`
- 同步代码改动：`feat: ...` 或 `fix: ...`

### 步骤 6 — 是否需要打 tag

询问用户：「这次改动是否需要打 vX.Y tag？」
- 累积多次小改动后再打 tag 比较合理
- 单纯加一张卡通常不需要打 tag

如要打 tag，按 v0.1 / v0.2 / v0.3 的递增规律：

```bash
git tag -a v0.4 -m "Release v0.4: <主题>"
git push origin v0.4
```

同时记得更新 `profiles.json` 中的 `tool_version` 字段，再重新加密一次。

## 注意事项

1. **不要把 profiles.json 提交到仓库** —— 它在 .gitignore，但 `git add -A` 或粗心 `git add .` 不会越过 .gitignore，问题不大；但用 `git add profiles.json` 强制添加会绕过 gitignore，**禁止这样做**。

2. **不要更换加密密钥** —— 除非明确用户要求。密钥变了，老的 profiles.enc 就读不出来了，所有用旧版本的部署会失败。

3. **远端测试是必经环节** —— 因为加密/解密链路若中间任何一步出错（比如手抖修改了 `_PROFILE_KEY`、改坏了 `decode_profile_bytes`），本地编译能通过但远端运行会失败。本地 Windows 受平台 guard 影响，跑不全脚本，只能远端验证。

4. **修改 inspector.py 中的加密相关函数要极谨慎** —— 一旦 encode/decode 算法变了，旧的 .enc 文件失效。如果必须变，要在同一次提交里同时更新 inspector.py 和重新生成 profiles.enc。

5. **报告中性能数字异常时优先检查 nvidia_equivalent** —— 大多数性能估算异常都来自对标系数或对标卡配错。`_lookup_nvidia_perf` 在找不到 tier 时会 fallback 到相邻档位，但 `nvidia_equivalent.card` 字段拼错会让整个对标失效，落入保守默认值（8-18 tok/s 这种）。

6. **profiles.json 的 schema 关键字段**（修改时务必保留结构）：
   - 顶层：`tool_version`, `bin_search_paths`, `bin_categories`, `cpu_vendor_x86`, `cpu_model_keywords`, `accelerators`, `models_2026`, `scenario_templates`, `nvidia_reference_benchmarks`
   - 每个 accelerator 必须有：`id`, `display_name`, `detect_smi`, `model_specs`, `default_spec`
   - 每个 model_spec 推荐有：`match`, `bf16_tflops`, `hbm_gb`, `hbm_bw_tbps`, `nvidia_equivalent`
   - `nvidia_equivalent` 结构：`{"card": "<NVIDIA card key>", "perf_ratio": 0.xx, "note": "可选"}`

## 快捷参考：常见任务模板

### 添加新加速器型号到现有厂商

在对应 `accelerators[].model_specs[]` 数组里插入一行，对照同厂商已有条目的字段格式。

例：在 hygon 下添加 K100-Pro：
```json
{"match": "K100-Pro", "bf16_tflops": 240, "int8_tops": 480, "hbm_gb": 96, "hbm_bw_tbps": 1.6, "tdp_w": 400, "pcie": "PCIe 5.0", "interconnect": "Hygon Link", "nvidia_equivalent": {"card": "A100-80GB-SXM", "perf_ratio": 0.55}}
```

### 添加新厂商

复制 `cambricon` 或 `biren` 整段做模板，按需修改字段。注意 `default_spec` 必须有 `nvidia_equivalent`，否则性能估算会落 fallback。

### 调整对标系数

只改 `nvidia_equivalent.perf_ratio` 一个数即可。配合 note 字段说明依据。

### 更新 NVIDIA 基准数据

修改 `nvidia_reference_benchmarks.cards.A100-80GB-SXM` 下的 `single_card` / `single_node_8` / `multi_node_16` 三个对象。其他 NVIDIA 卡通过 `vs_a100_multiplier` 联动，无需重复维护。

### 加新模型

在 `models_2026` 数组追加。注意 `active` 字段会决定性能档位（`_active_param_tiers` 中 tiny/small/medium/large 的边界），写实际激活参数量。

## 验证 checklist（提交前自查）

- [ ] `profiles.json` 是合法 JSON（`json.load` 无报错）
- [ ] `profiles.enc` 已重新生成
- [ ] 远端运行 `inspector.py` 无报错
- [ ] 如改动涉及报告，已查看一次新报告确认改动生效
- [ ] `git status` 中没有 `profiles.json`（应在 .gitignore 内）
- [ ] commit message 描述清楚改动内容和原因
