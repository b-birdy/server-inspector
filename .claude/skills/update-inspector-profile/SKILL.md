---
name: update-inspector-profile
description: 维护 server-inspector 项目的加密配置文件 profiles.enc。三种工作模式：(A) 日常编辑数据库（加新卡、调系数、改模型清单）；(B) 把用户上报的贡献 JSON 合并进数据库；(C) 从备份还原 profiles.json。任何动 profiles.* 内容的需求都走这套流程。
---

# 更新 server-inspector 加密配置流程

## 何时调用此 skill

按模式分：

**模式 A — 日常编辑数据库**（最常见）。用户说：
- 「加一张新卡：XXX」/「补充某型号规格」
- 「调整 P800 对标系数为 0.55」
- 「更新 NVIDIA 基准数据」
- 「新增 2027 年发布的模型 XX」
- 「修改 single_node_8 场景的匹配规则」

**模式 B — 合并用户贡献**。用户说：
- 「这是 XXX 上报的硬件数据，看看能不能合进去」
- 「review 一下这个贡献 JSON」
- 「合并这个新硬件到数据库」
- 给你一个 JSON 文件路径或粘贴 JSON 内容

**模式 C — 从备份还原**。用户说：
- 「数据库改坏了，还原到上一版」
- 「列一下 backups 看看」
- 「回滚 profiles 到昨天那个版本」

凡是动 `profiles.*` 文件内容的需求都走这个 skill。

## 项目关键信息

- 仓库本地：`C:\Users\Administrator\coding-project\server-inspector\`
- 远端测试机（昆仑芯 P800）SSH 别名：`my-server`
- GitHub：`git@github.com:b-birdy/server-inspector.git`
- Gitee 镜像：`git@gitee.com:wzxdcyy/server-inspector.git`
- 加密密钥（硬编码在 inspector.py，**不要换**）：`svi-internal-2026-pkg-v1`
- 加密算法：`zlib 压缩 → 固定 key XOR → Base64`（对称，详见 `encode_profile_bytes` / `decode_profile_bytes`）
- 本地备份目录：`backups/profiles_<YYYYMMDD-HHMMSS>.json`（.gitignore，不进仓库）
- 用户贡献文件来源：用户机器上的 `~/.server-inspector/contribute/<ts>_<host>.json`

---

## 模式 A：日常编辑数据库

### 步骤 A1 — 确认/恢复明文 profiles.json

```bash
cd /c/Users/Administrator/coding-project/server-inspector
ls profiles.json 2>/dev/null && echo "明文已存在" || echo "需要解密"
```

- **明文存在**：进入 A2
- **不存在**（刚 clone 或被清理过）：从 .enc 解密：

```bash
cd /c/Users/Administrator/coding-project/server-inspector && python -c "
import sys; sys.path.insert(0, '.')
from inspector import decode_profile_bytes
from pathlib import Path
Path('profiles.json').write_bytes(decode_profile_bytes(Path('profiles.enc').read_bytes()))
print('Decoded profiles.json restored')
"
```

> `profiles.json` 在 `.gitignore`，只是本地工作副本。

### 步骤 A2 — 编辑前自动备份

**改 profiles.json 之前必须做**。备份失败就停下，不要继续。

```bash
cd /c/Users/Administrator/coding-project/server-inspector
mkdir -p backups
TS=$(date +%Y%m%d-%H%M%S)
cp profiles.json "backups/profiles_${TS}.json"
echo "✓ 备份: backups/profiles_${TS}.json"
ls -lt backups/ | head -6
```

老备份不要手动删；定期清理可以保留最近 30 个，但人工判断。

### 步骤 A3 — 编辑 profiles.json

用 Edit / Write 工具按用户需求改 `profiles.json`。

**编辑后立刻 JSON 校验**：

```bash
python -c "import json; json.load(open(r'C:\Users\Administrator\coding-project\server-inspector\profiles.json', encoding='utf-8')); print('JSON OK')"
```

格式错误必须立刻修复，**不要继续后续步骤**。

### 步骤 A4 — 重新生成 profiles.enc

```bash
cd /c/Users/Administrator/coding-project/server-inspector && python inspector.py --encode-profile profiles.json
```

预期输出：`[OK] 已生成加密配置: profiles.enc (XXXX bytes)`。文件大小变化是正常的。

### 步骤 A5 — 远端测试验证（不可跳过）

P800 是参考实现，任何 profile 改动都要在 P800 上跑一次确认不破坏现有行为：

```bash
scp /c/Users/Administrator/coding-project/server-inspector/inspector.py \
    /c/Users/Administrator/coding-project/server-inspector/profiles.enc \
    my-server:~/server-inspector/
ssh my-server "cd ~/server-inspector && python3 inspector.py --output-dir ./reports 2>&1 | tail -15"
```

如果改动涉及报告渲染（模型清单、对标系数、场景模板等），把最新报告回传查看：

```bash
latest=$(ssh my-server "ls -t ~/server-inspector/reports/*.md | head -1")
scp my-server:"$latest" /c/Users/Administrator/coding-project/server-inspector/reports/verify_latest.md
```

阅读 `verify_latest.md`，检查 P800 仍然是 8 卡 / 768 GB / TP=8 / RDMA 11 路 Active / 性能 3 场景齐全。

### 步骤 A6 — Git 提交

**只提交 `profiles.enc`**（`profiles.json` 已在 .gitignore）。如同时改了 `inspector.py` 一起加：

```bash
cd /c/Users/Administrator/coding-project/server-inspector
git status -s   # 必须看不到 profiles.json
git add profiles.enc [inspector.py]
git commit -m "data: <一句话描述改动>

<具体改了什么、为什么改>"
git push origin master
```

commit message 风格：
- 配置数据更新：`data: 添加 XX 卡规格`、`data: 调整 P800 对标系数`
- 同步代码改动：`feat: ...` 或 `fix: ...`

### 步骤 A7 — 是否打 tag

询问用户：「这次改动是否需要打 vX.Y tag？」

- 单纯加一张卡通常**不打 tag**
- 累积多次后或修了重要 bug 才打 tag
- 打 tag 必须先升 `profiles.json` 的 `tool_version` 字段，**重新加密**，再 commit

```bash
git tag -a v0.X -m "Release v0.X: <主题>"
git push origin v0.X
```

### 步骤 A8 — 同步到 Gitee

打了 tag 或重要 master 推送后，调用用户级 skill `sync-to-gitee` 同步双仓库。

---

## 模式 B：合并用户贡献到数据库

用户给你一份贡献 JSON（通常路径是 `~/.server-inspector/contribute/<ts>_<host>.json`，或者直接粘贴内容）。任务是 review 这份数据，决定是否合并进 profiles.json。

### 步骤 B1 — 读取贡献文件

```bash
# 如果用户给了路径
cat <用户提供的路径>

# 用户也可能直接粘贴 JSON 内容到对话里
```

### 步骤 B2 — 分析未识别项

JSON 顶层 `unknown_items` 数组列出了用户环境里没被识别的硬件。逐项分析：

| `type` | 检查点 |
|--------|--------|
| `gpu`（lspci 检测到加速器但厂商 SMI 未命中） | 看 `details.pcie_devices`，提取 vendor ID（如 `[10de:xxxx]`）。是已知厂商但新型号？新厂商？还是 PCIe class 误识别？|
| `gpu`（厂商识别但 model_name 未匹配 model_specs） | 看 `details.smi_summary` 里 SMI 输出，提取真实型号字符串。补 `model_specs[]` entry 即可 |
| `cpu`（厂商未识别） | 看 `details.model` 字段，对照 `cpu_vendor_x86` / `cpu_model_keywords` 看缺哪条 |

### 步骤 B3 — 查公开技术参数

对于要新增的硬件，从公开来源（厂商白皮书 / Wikipedia / TechPowerUp）查：

- GPU：FP32 / FP16 / BF16 / INT8 / INT4 TFLOPS（dense），HBM 容量/带宽/类型，TDP，PCIe gen，互联
- CPU：vendor，arch，常见型号 keyword

查不到的字段：
- 不支持的精度（如 Turing 的 BF16） → 写 `0`（渲染"不支持"）
- 缺数据 → 省略字段（渲染"暂无数据"）

如果是国产卡且公开数据稀缺，**对标 NVIDIA 卡的 perf_ratio 是最关键的字段**（必须有，否则性能预估走 fallback）。

### 步骤 B4 — 准备 patch 给用户确认

把要新增/修改的 entry 用文字列出来给用户看，**等用户确认后再改文件**。例如：

> 准备新增 NVIDIA RTX 5060 Ti（来自用户 xxxxx 的贡献）：
> ```json
> {"match": "RTX 5060 Ti", "fp32_tflops": ..., ...}
> ```
> 同时在 `nvidia_reference_benchmarks.cards` 加 `"RTX-5060-TI": {"vs_a100_multiplier": 0.15}`
>
> 是否合并？

### 步骤 B5 — 用户确认后，走模式 A 闭环

确认后，按模式 A 的 A2-A8 走完：备份 → 编辑 → 加密 → 远端测试 → 提交。

commit message 提一句贡献来源（不暴露 hostname/IP 等用户隐私）：
```
data: 新增 RTX 5060 Ti 规格（来自社区贡献）

补全 FP32/FP16/INT8 算力 / 16GB GDDR7 显存 / 180W TDP / PCIe 5.0 x8。
NVIDIA reference 表加 vs_a100_multiplier=0.15。
```

### 步骤 B6 — 通知用户

合并完成后告诉用户：
- 哪些 entry 已加入
- 在 v0.X tag 之后会跟随发布
- 感谢贡献

---

## 模式 C：从备份还原 profiles.json

### 步骤 C1 — 列出可用备份

```bash
cd /c/Users/Administrator/coding-project/server-inspector
ls -lt backups/profiles_*.json 2>/dev/null | head -10
```

如果列表为空 → 没有备份可还原，告诉用户考虑从历史 commit 中恢复 profiles.enc 然后解密。

### 步骤 C2 — 让用户选

把列表展示给用户，请他选一份。每份文件名带时间戳，能识别。

如果用户不确定，可以 diff 当前 profiles.json 和备份的差异，帮他判断：

```bash
diff profiles.json backups/profiles_<TS>.json | head -50
```

### 步骤 C3 — 备份当前，还原选中

**还原前先备份当前**（即使要丢弃，留一份避免误操作）：

```bash
cd /c/Users/Administrator/coding-project/server-inspector
TS=$(date +%Y%m%d-%H%M%S)
cp profiles.json "backups/profiles_${TS}_before_restore.json"
cp "backups/profiles_<USER_SELECTED>.json" profiles.json
echo "✓ 已还原；当前版本备份为 backups/profiles_${TS}_before_restore.json"
```

### 步骤 C4 — 验证 + 加密 + 测试 + 提交

按模式 A 的 A3-A6 走：JSON 校验 → 加密 → 远端测试 → 提交。commit message：

```
data: 回滚 profiles.json 到 <YYYYMMDD-HHMMSS> 备份

<回滚原因>
```

---

## 注意事项

1. **绝不 `git add profiles.json`**：.gitignore 拦不住手动强加。务必只 `git add profiles.enc [inspector.py]`。

2. **绝不换加密密钥** `_PROFILE_KEY`：变了所有旧 .enc 失效，已部署的用户全部崩。除非用户明确要求且做好版本切换方案。

3. **远端测试是必经环节**：本地 Windows 受平台 guard 影响跑不全脚本，加密/解密链路、PCIe 解析、SMI 调用都只能在 Linux 跑。my-server (P800) 是参考回归基准。

4. **修 inspector.py 中加密函数要极谨慎**：`encode_profile_bytes` / `decode_profile_bytes` 改了，旧 .enc 失效。必须同一次提交里同时改 inspector.py 和重新生成 profiles.enc。

5. **性能数字异常优先查 `nvidia_equivalent`**：`_lookup_nvidia_perf` 在找不到对应 cards key 时会走 `_resolve_cards_key` 模糊匹配，仍找不到才 fallback 到保守默认（8-18 tok/s 这种很低的数）。新增 NVIDIA 卡必须在 `nvidia_reference_benchmarks.cards` 加对应 `vs_a100_multiplier`。

6. **不要提交贡献文件原文到仓库**：用户发来的 JSON 含 hostname 等信息，仓库公开。Review 完合并到 profiles.json 即可，贡献文件本身留本地或在 commit message 里简单引用就行。

7. **profiles.json schema 关键字段**（动了要确保结构不破坏）：
   - 顶层：`schema_version`, `tool_version`, `contribution`, `bin_search_paths`, `bin_categories`, `cpu_vendor_x86`, `cpu_model_keywords`, `accelerators`, `models_2026`, `scenario_templates`, `nvidia_reference_benchmarks`
   - 每个 accelerator 必须有：`id`, `display_name`, `detect_smi`, `model_specs`, `default_spec`
   - 每个 model_spec 推荐有：`match`, `fp32_tflops`, `fp16_tflops`, `bf16_tflops`, `int8_tops`, `hbm_gb`, `hbm_bw_tbps`, `tdp_w`, `pcie`, `interconnect`, `memory_type`, `nvidia_equivalent`
   - 不支持的精度写 `0`（渲染"不支持"），缺数据省字段（渲染"暂无数据"）
   - `nvidia_equivalent` 结构：`{"card": "<NVIDIA card key>", "perf_ratio": 0.xx, "note": "可选"}`

## 快捷参考：常见任务模板

### 添加新加速器型号到现有厂商

在对应 `accelerators[].model_specs[]` 数组里插入一行，对照同厂商已有条目的字段格式。**长字符串在前**（避免 RTX 4090 截胡 RTX 4090 D）。

例：在 hygon 下添加 K100-Pro：
```json
{"match": "K100-Pro", "fp32_tflops": 60, "fp16_tflops": 240, "bf16_tflops": 240, "int8_tops": 480, "hbm_gb": 96, "hbm_bw_tbps": 1.6, "tdp_w": 400, "pcie": "PCIe 5.0", "interconnect": "Hygon Link", "memory_type": "HBM3", "nvidia_equivalent": {"card": "A100-80GB-SXM", "perf_ratio": 0.55}}
```

### 添加新厂商

复制 `cambricon` 或 `biren` 整段做模板，按需修改字段。注意：
- `default_spec` 必须有 `nvidia_equivalent`，否则性能估算走 fallback
- `lspci_pattern` 用 vendor ID（如 `1ec9:`），**不要用厂商名**（容易和 CPU vendor 字符串撞）
- SMI 命令名加入 `bin_categories.accelerator_smi`，让 CommandRegistry 能发现
- SMI 装在非标准目录时把目录加入 `bin_search_paths`

### 调整对标系数

只改 `nvidia_equivalent.perf_ratio` 一个数即可。配合 `note` 字段说明依据。

### 更新 NVIDIA 基准数据

修改 `nvidia_reference_benchmarks.cards.A100-80GB-SXM` 下的 `single_card` / `single_node_8` / `multi_node_16` 三个对象。其他 NVIDIA 卡通过 `vs_a100_multiplier` 联动，无需重复维护。

### 加新 NVIDIA 卡到 cards 表

```json
"RTX-5060-TI": {"_doc": "...", "vs_a100_multiplier": 0.15}
```

key 用 hyphen 大写风格（与 spec.match 的空格风格不同，但 `_resolve_cards_key` 会做归一化匹配）。

### 加新模型

在 `models_2026` 数组追加。注意：
- `active` 字段决定性能档位（`_active_param_tiers` 中 tiny/small/medium/large 边界）
- 三个量化档显存都要给：`fp16_gb` / `int8_gb`（约 fp16 的一半）/ `int4_gb`

## 验证 checklist（提交前自查）

- [ ] 编辑前已 `cp profiles.json backups/profiles_<TS>.json`
- [ ] `profiles.json` 是合法 JSON（`json.load` 无报错）
- [ ] `profiles.enc` 已重新生成
- [ ] 远端 P800 运行 `inspector.py` 无报错，关键字段（8 卡 / TP=8 / 性能场景齐全）无回归
- [ ] 如改动涉及报告，已查看 verify_latest.md 确认改动生效
- [ ] `git status` 中**没有** `profiles.json`
- [ ] commit message 描述清楚改动内容和原因
- [ ] 如打 tag，已升 `tool_version` 并重新加密
- [ ] 如是贡献合并，已感谢贡献者（在回复用户时）
