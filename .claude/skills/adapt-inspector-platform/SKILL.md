---
name: adapt-inspector-platform
description: 给 server-inspector 适配新平台 / 新加速卡 / 新容器环境的完整闭环。当用户说「在 X 平台测了有问题 / 看下报告」「加 X 新卡」「X 容器跑不对」「X 卡识别错了」时调用。负责从用户提供的报告和 SMI 原始输出 → 诊断 → 改代码+数据库 → 本地验证 → P800 回归 → 新平台复测 → 发布的完整 7 阶段流程。
---

# 适配新平台 / 新卡型 / 新环境到 server-inspector

## 何时调用此 skill

用户说出下列任一类话时触发：

- 「在 XX 平台测了，[卡数 / 显存 / 算力 / 框架] 显示不对」
- 「我换了张新卡 / 接入了 XX 服务器，识别有问题」
- 「在 XX 容器 / 虚机里跑，[xxx] 异常」
- 「加 XX 型号到数据库」
- 「XX 卡的 [BF16 / INT8 / 显存带宽 / TDP] 显示不准」
- 「报告里的 [某字段] 明明 X，显示成 Y」

**反例**：纯数据微调（如「把 P800 的 perf_ratio 改成 0.55」）→ 直接调用 `update-inspector-profile`，不需要走这个完整 7 阶段。

## 项目关键信息

- 仓库本地：`C:\Users\Administrator\coding-project\server-inspector\`
- GitHub：`git@github.com:b-birdy/server-inspector.git`
- Gitee 镜像：`git@gitee.com:wzxdcyy/server-inspector.git`
- 远端测试机（昆仑芯 P800）SSH 别名：`my-server`
- 加密密钥：`svi-internal-2026-pkg-v1`（**不要改**）
- 当前版本：v0.8（每次发布递增）

## 7 阶段工作流

### 阶段 1 — 收集信息

**必备**：用户提供新平台跑出来的报告 .md 文件（路径或粘贴内容）。

**按现象按需让用户跑命令并贴回**（不要让用户跑无关命令污染会话）：

| 厂商 / 场景 | 核心命令 |
|------------|---------|
| 通用（所有卡） | `lspci -nn`（关键，看 PCIe class code 和 vendor ID） |
| NVIDIA | `nvidia-smi`, `nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv` |
| 昇腾 | `npu-smi info`, `npu-smi -v`, `npu-smi info -t memory -i 0`, `npu-smi info -t board -i 0`, `npu-smi info -l` |
| 昆仑 | `xpu-smi`, `xpu-smi -q | head -50` |
| 海光 DCU | `hy-smi`, `dtk-smi`, `hy-smi --showproductname` |
| AMD ROCm | `rocm-smi`, `rocm-smi --showproductname`, `amd-smi static --asic` |
| 寒武纪 | `cnmon`, `cnmon info | head -60` |
| 壁仞 / 沐曦 / 摩尔 / 天数 | `biren-smi` / `mx-smi` / `mthreads-gmi` / `ixsmi` |
| Habana | `hl-smi`, `hl-smi -q | grep -i version` |
| 容器/驱动 | `cat /etc/hostname`, `ls /usr/local/Ascend/driver/version.info`, `cat /.dockerenv` |
| RDMA | `ibstatus`, `ibstat`, `show_gids` |

**如果是全新卡型**，让用户提供公开技术参数（厂商白皮书或维基数据）：
- FP32 TFLOPS（dense）
- FP16 TFLOPS（Tensor Core，dense；不支持写 0）
- BF16 TFLOPS（Tensor Core，dense；不支持写 0，如 Turing）
- INT8 TOPS（dense；不支持写 0）
- INT4 TOPS（dense；Ada/Hopper/Blackwell 取消原生 INT4 时不填字段）
- HBM/显存容量 GB、带宽 TB/s、类型（HBM3 / GDDR7 等）
- TDP W、PCIe gen、互联方式（NVLink / HCCS / XPU Link 等）

### 阶段 2 — 诊断报告异常（按 checklist 比对）

读用户的报告 `.md`，逐项核对：

| 异常 | 报告里的信号 | 大概率根因 | 修哪里 |
|------|------------|----------|--------|
| 卡型未识别 | 卡型号 = "未识别"；BF16/HBM 全为 0/未知 | `lspci_pattern` 不匹配 vendor ID；或 `model_specs[].match` 不含该型号 | `profiles.json` accelerator 的 `lspci_pattern` 收窄 / `model_specs[]` 新增 entry |
| 卡数错误（多算 2N/3N） | "× 4 卡" 实际 2 卡 | `_parse_accel_specs` 对应分支把多个 SMI 输出 join 后 count 同一字段（如"NPU ID :"出现在 overview / mem / board 各一次） | `inspector.py:_parse_accel_specs` 改成 set 去重 / 只在 overview 单源数 |
| 卡数错误（误识别桥接器） | "× 149 卡，单卡 0 GB"（AMD CPU 桥被算成 GPU） | `lspci_pattern` 写得太宽 + 缺 PCIe class 白名单 | `_is_accel_pcie_line` 白名单（已有，03/12/0b40/1180）；`lspci_pattern` 用 vendor ID（如 `1002:`） |
| 显存为 0 / 误差大 | "单卡显存: 未知" 或与实测差距数倍 | SMI 解析正则未命中；或 SMI 命令在该版本不存在/输出格式变了 | 加 spec.hbm_gb 兜底（`single_mib == 0` 才用）+ 改进解析正则 |
| 显存"被回退" | 魔改卡 / vGPU 实际显存与 spec 标称不同 | **不要加** sanity check 回退（这是历史 bug） | 始终信任 smi 实测值。"性能及规格参考"表会展示标称+实测对照 |
| 算力字段为 0 但实际支持 | "BF16 算力 0 TFLOPS" 但卡实际支持 | `model_specs[].bf16_tflops` 缺失或写 0 | 补真实值；不支持的精度（如 Turing 的 BF16）显式写 0 → 报告渲染"不支持" |
| 算力字段显示"暂无数据" | 字段缺失 | 该 spec 没填 fp32/fp16 等字段 | 补真实值 |
| 性能预估"未匹配到适用场景" | 第 6 章空 | 通常是显存=0（解决显存后自动好），或 `nvidia_equivalent` 缺（NVIDIA 自家有特判，国产卡必须显式） | 先解决显存 → 还不行再加 nvidia_equivalent |
| 性能数字过高/过低（10x 级离谱） | 总吞吐异常 | `nvidia_equivalent.perf_ratio` 不合理 / `vs_a100_multiplier` 不合理 / 缩放系数错 | 查 nvidia_reference_benchmarks 对应数据 |
| 框架检测漏 | "ML 推理框架"表少了某框架（如 torch_npu/vllm_ascend） | `collect_ml_frameworks` 的 pkg 列表没列 | inspector.py: `collect_ml_frameworks` 加 `(pkg_name, label)` |
| 驱动版本检测漏 | "驱动程序"表少某项 | `collect_drivers` 没检测对应路径 | inspector.py: `collect_drivers` 加 cat / 命令 |
| 容器内"主机:"为空 | 报告头部 `> **主机:** ``` | `hostname -f` 在容器内失败 | 已有 `ReportGenerator.__init__` 兜底；若仍空，排查 collect_os |
| 容器内误报"无 GPU 容器工具" | warning 章节出现 | 容器内不该报 | 已用 `in_container` 跳过；若新容器仍报，确认 runtime_env 检测命中 docker/k8s |
| RDMA 工具自相矛盾 | "工具完整" vs 表格"性能测试: 无" | sanity_check 老逻辑无脑判 | 已细化为"基础 / perftest / 诊断"分项显示 |
| 网卡接口"无法获取" | 容器内 ip 命令受限 | 已知限制 | 可加 `/proc/net/dev` 兜底，但优先级低 |
| 部署建议 TP 数错 | 实际 2 卡显示 TP=8 | 卡数错误的级联（修 #2 后自动好） | 不要单独改 recommend() |

### 阶段 3 — 修改数据库 + 代码

**纯数据**（新型号、调系数、新模型）：编辑 `profiles.json`。

**新厂商**（首次接入某品牌的 SMI 工具/检测路径）：
1. `profiles.json` 在 `accelerators[]` 复制现有厂商整段做模板（推荐用 cambricon 或 biren 当模板）
2. 修改 `id` / `display_name` / `vendor` / `detect_smi` / `detect_smi_keyword` / `lspci_pattern` / `queries{}` / `frameworks[]` / `model_specs[]` / `default_spec{}`
3. **`default_spec` 必须有 `nvidia_equivalent`**，否则性能预估直接 fallback 到保守默认
4. 把 SMI 命令名加入 `bin_categories.accelerator_smi`，让 CommandRegistry 能发现
5. 如 SMI 装在非标准目录（如 `/usr/local/Ascend/driver/tools`），把目录加进 `bin_search_paths`

**新代码逻辑**（新厂商的特殊解析）：编辑 `inspector.py:_parse_accel_specs`，复制现有 `elif accel_id == "xxx":` 分支做模板，按该厂商的 SMI 输出格式写正则。

**框架/驱动检测**：改 `collect_ml_frameworks` / `collect_drivers`。

### 阶段 4 — 本地验证

```bash
cd /c/Users/Administrator/coding-project/server-inspector

# 1. JSON 合法
python -c "import json; d=json.load(open('profiles.json',encoding='utf-8')); print('JSON OK, version:', d['tool_version'])"

# 2. Python 语法
python -c "import ast; ast.parse(open('inspector.py',encoding='utf-8').read()); print('inspector.py OK')"

# 3. 解析单元测试（针对复杂厂商分支）
# 创建 _test_<vendor>.py，用用户提供的 SMI 原始输出当 fixture
# 调用模拟的 _parse_accel_specs，验证 (card_count, single_mib, model_name) 都正确
# 测试通过后 rm 删掉，不要 commit
```

### 阶段 5 — P800 远端回归（不可跳过）

**这一步保证不破坏其他平台**。任何代码改动都要在 P800 上跑一遍验证：

```bash
cd /c/Users/Administrator/coding-project/server-inspector
python inspector.py --encode-profile profiles.json
scp profiles.enc inspector.py my-server:~/server-inspector/
ssh my-server "cd ~/server-inspector && python3 inspector.py --output-dir ./reports 2>&1 | tail -15"
```

确认 P800 报告：
- 8 卡 / 单卡 96 GB / 总 768 GB
- 部署 TP=8
- RDMA 11 路 Active 400 Gb/s
- 性能预估 3 场景全输出（单卡 / 单机多卡 2-8 / 双机 16）

如有偏差，**先修回归再继续**。

### 阶段 6 — 让用户在新平台再跑一次

```
让用户：
1. 在新平台拉 master 或对应 tag：
   curl -fsSL https://gitee.com/wzxdcyy/server-inspector/raw/master/install.sh | bash
   server-inspector
2. 把新报告 .md 贴回来
3. 再读一遍核对：每个之前发现的异常都应该消失
```

如果还有未修复项，回阶段 2。

### 阶段 7 — 发布

1. **升 tool_version**（profiles.json 顶层，v0.X → v0.X+1）
2. **重新加密**：`python inspector.py --encode-profile profiles.json`
3. **commit**：只 add `inspector.py` 和 `profiles.enc`（profiles.json 在 .gitignore），commit message 描述本次新增支持的平台/卡型 + 修复的具体问题
4. **打 tag**：`git tag -a v0.X -m "..."` + `git push origin v0.X`
5. **同步 Gitee**：调用 `sync-to-gitee` skill

详细加密/提交流程见 `update-inspector-profile` skill。

## 数据库 schema 速查

```
profiles.json
├── tool_version                          ← 每次发布必升
├── bin_search_paths[]                    ← 加新厂商工具所在的非标准目录
├── bin_categories.accelerator_smi[]      ← 加新 SMI 命令名（reg.has() 才能发现）
├── accelerators[]                        ← 新厂商在这加
│   ├── id, display_name, vendor
│   ├── detect_smi, detect_smi_keyword
│   ├── lspci_pattern                     ← 用 vendor ID 不要用厂商名（如 1002: 不是 advanced micro）
│   ├── queries{}                         ← SMI 命令字符串
│   ├── frameworks[]                      ← 推荐顺序
│   ├── model_specs[]                     ← 长字符串在前（"RTX 4090 D" 必须排在 "RTX 4090" 前）
│   │   └── 每条字段：match / fp32_tflops / fp16_tflops / bf16_tflops / int8_tops / int4_tops
│   │                / hbm_gb / hbm_bw_tbps / memory_type / tdp_w / pcie / interconnect
│   │                / nvidia_equivalent {card, perf_ratio, note}
│   └── default_spec{}                    ← 必须有 nvidia_equivalent
├── models_2026[]                         ← 模型清单
│   └── fp16_gb / int8_gb / int4_gb       ← 三档量化显存都要有
├── scenario_templates[]
│   ├── single_card (1+)
│   ├── single_node_8 (2-8 卡)            ← TP=N 自适应，< 8 卡按 actual/8 缩放
│   └── multi_node_16 (≥8 卡 + RDMA)
└── nvidia_reference_benchmarks
    ├── _active_param_tiers               ← tiny(1-5B) / small(6-15) / medium(16-35) / large(36-80)
    ├── _scaling_efficiency               ← 单卡 1.0 / 单机 8 卡 0.78 / 双机 16 卡 0.55
    └── cards{}                           ← A100 是基准（有完整 3 场景 × 4 档基准数据）
                                            其他卡只需 vs_a100_multiplier
```

## 代码 hotspots

| 位置 | 用途 | 改动场景 |
|------|------|---------|
| `_is_accel_pcie_line` (模块顶部) | PCIe class 白名单 | 新加速器用了非常规 class（很少见） |
| `_parse_accel_specs` (HardwareCollector) | 每厂商一个 elif 分支 | 新厂商时复制现有分支做模板 |
| `_match_model_spec` | 归一化匹配 model_specs | 一般不动 |
| `_lookup_nvidia_perf` / `_resolve_cards_key` | NVIDIA 基准查找 | 一般不动 |
| `_estimate_perf` | 性能估算公式 + 缩放 | 一般不动 |
| `collect_drivers` | 驱动版本检测 | 新厂商驱动版本路径 |
| `collect_ml_frameworks` | pip 框架检测 | 新框架 |
| `collect_rdma_cluster` | RDMA / MPI / CCL | 新厂商集合通信库 |
| `evaluate_models` | 模型兼容性状态 | 改判定逻辑（很少） |
| `sanity_check` | 警告/正常项 | 新厂商首选框架判定 |
| `recommend` | 部署建议 | 一般不动（跟 accel_count 走） |
| `ReportGenerator.__init__` | 主机名兜底 | 一般不动 |

## 历史 bug 教训（必须遵守的原则）

1. **显存策略：smi 实测值优先，不要因偏差大就回退**。只在 `single_mib == 0` 时用 `spec.hbm_gb` 兜底。魔改卡 / vGPU / SR-IOV 切片场景下，smi 报的就是真实可用值。
2. **卡数不要 join 多源 SMI 输出后 count**。同一张卡的 `NPU ID :` 字段会在 overview / `-t memory` / `-t board` 各出现一次，叠加导致 2N/3N。用 `set` 去重，或只在 overview 数型号关键字。
3. **lspci_pattern 用 vendor ID 不用厂商名**。AMD CPU vendor 0x1022 / AMD GPU vendor 0x1002，写 `1002:` 而不是 `advanced micro`（会误食 CPU 桥接器）。
4. **PCIe class 白名单做兜底保险**。`_is_accel_pcie_line` 白名单 `03|12|0b40|1180`，即使将来 lspci_pattern 写宽也不会误食桥接器/IOMMU/USB。
5. **model_specs 长字符串在前**。`RTX 4090 D` 排 `RTX 4090` 之前，否则被截胡。
6. **不支持的精度写 0，缺数据省字段**。Turing 的 BF16 → `bf16_tflops: 0`（渲染"不支持"）；没查到数据 → 不写字段（渲染"暂无数据"）。
7. **NVIDIA cards 表 key 用 hyphen 风格**（"RTX-4090"），spec.match 用空格风格（"RTX 4090"），代码 `_resolve_cards_key` 做模糊匹配。混用容易出 bug。
8. **报告不暴露对标算法**。性能预估文案只说"基于公开技术参数和行业推理基准的理论推算"，不提 A100/H100/perf_ratio/vs_a100。
9. **状态描述带数字原因**。模型兼容性"状态"列不要只说"⚠️ 仅满足量化部署"，要带"总显存 64GB 不够 BF16（需 70GB），可上 INT8 量化（需 35GB）"。
10. **未匹配场景隐藏性能指标说明**。第 6 章没数据时，下面"性能指标说明"块也跟着不显示。
11. **容器内不报"无 GPU 容器工具"**。`当前脚本运行环境 != "宿主机"` 跳过。
12. **首选框架判定用 OR 不用单个**。昇腾 = MindSpore / MindIE / vLLM-Ascend / LMDeploy / torch_npu **任一**存在就通过。
13. **加密密钥不能换**。换了所有旧 .enc 失效，所有用户部署崩。
14. **profiles.json 不能提交**。它在 .gitignore，但禁止 `git add profiles.json` 强加。

## 触发其他 skill 的时机

| 时机 | 调用 |
|------|------|
| 改完 `profiles.json` 要重新加密 + 远端验证 + 提交 | `update-inspector-profile`（项目内） |
| 发布到 Gitee | `sync-to-gitee`（用户级） |

## 提交前 checklist

- [ ] 用户提供的新平台报告所有异常已修
- [ ] JSON 合法、Python 语法 OK
- [ ] P800 远端回归通过（8 卡 / 96 GB / TP=8 / RDMA / 3 场景全输出）
- [ ] 新平台用户上传新报告确认通过
- [ ] `tool_version` 已升
- [ ] `profiles.enc` 重新生成（大小变化是正常的）
- [ ] commit message 描述新增支持的平台/卡型 + 具体修复
- [ ] tag（如果是发布版本）
- [ ] Gitee 同步完成
- [ ] 任何临时 `_test_*.py` fixture 已删除
