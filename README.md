# 🔍 Server Inspector

**自动评估服务器的大模型推理能力。**

一行命令，生成硬件 + 推理性能评估报告（Markdown / HTML / JSON）。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.6+](https://img.shields.io/badge/Python-3.6+-blue.svg)](https://www.python.org/)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux-green.svg)](https://github.com/b-birdy/server-inspector)

---

一个通用的硬件容量评估工具，用于评估服务器在大语言模型推理部署中的性能表现。自动检测并分析CPU、内存、加速器、存储、网络和软件生态等硬件信息。

## ✨ 功能特性

- **多加速器支持**：支持11种以上GPU/加速器（NVIDIA、AMD、海光、昆仑、昇腾、浪潮、寒武纪、壁仞、沐曦、摩尔线程、Habana）
- **多架构CPU识别**：支持Intel x86、AMD、ARM（鲲鹏、飞腾）、RISC-V、龙芯、申威和Power等处理器架构
- **30+GPU型号规格库**：预配置的GPU型号数据，包含性能参数（TFLOPS）、显存（HBM）、带宽和功耗等指标
- **智能命令发现**：自动检测系统PATH和常见安装目录中的硬件诊断工具（nvidia-smi、rocm-smi、xpu-smi等）
- **性能场景建模**：评估单卡、单节点8卡、多节点16卡等不同部署场景下的推理性能
- **动态配置驱动**：硬件知识存储在外部`profiles.json`配置文件中，无需修改代码即可扩展新硬件
- **多种输出格式**：生成Markdown报告（易读）、HTML报告（可视化）和JSON数据（机器解析）
- **容器与Kubernetes检测**：识别Docker、containerd、Kubernetes以及GPU容器工具包
- **RDMA与集群工具包检测**：扫描多节点通信库和框架
- **ML框架检测**：识别系统中的PyTorch、TensorFlow、JAX等推理框架

## 🖥️ 支持的硬件

### GPU与加速器
- **NVIDIA**：支持所有CUDA设备（通过nvidia-smi）
- **AMD**：RDNA/CDNA芯片（通过rocm-smi）
- **海光**：DCU加速器（dcmi工具）
- **昆仑**：XTCPU/XTCPU-C系列（通过xpu-smi）
- **昇腾**：华为NPU（通过npu-smi）
- **浪潮**：专用GPU（通过iluvatar-smi）
- **寒武纪**：MLU加速器（通过cnmon）
- **壁仞**：SR系列（通过biren-smi）
- **沐曦**：MPS核心（通过metax-smi）
- **摩尔线程**：MTT系列（通过mtm-smi）
- **Habana**：Gaudi加速器（通过hl-smi）

### CPU处理器
- Intel（Xeon、Core系列）
- AMD（EPYC、Ryzen系列）
- Arm架构（鲲鹏、飞腾、安培）
- RISC-V架构处理器
- 龙芯处理器
- 申威处理器
- IBM Power处理器

## 🚀 一键安装

> 复制下面一行命令，粘贴到终端，回车——就够了。

```bash
curl -fsSL https://raw.githubusercontent.com/b-birdy/server-inspector/master/install.sh | bash
```

安装器会自动完成：
- **克隆最新代码**（优先 SSH → 自动切换 HTTPS）
- **创建命令软链接**（`server-inspector` 全局可用）
- **配置 PATH**（永久生效，写入 `~/.bashrc`）

---

### 系统要求

| 项目 | 要求 |
|------|------|
| 🐧 系统 | Linux（内核 3.10+） |
| 🐍 Python | 3.6 及以上（仅用标准库，无需 pip） |
| 📦 依赖 | 无外部依赖 |

---

### 方式一：一键安装（推荐）

```bash
# 安装
curl -fsSL https://raw.githubusercontent.com/b-birdy/server-inspector/master/install.sh | bash

# 立即使用（自动写入 PATH，重开终端后无需再执行）
source ~/.bashrc
server-inspector
```

### 方式二：手动安装

```bash
git clone https://github.com/b-birdy/server-inspector.git
cd server-inspector
python3 inspector.py --output-dir ./reports
```

---

## ⚡ 快速开始

```bash
# 默认配置，直接运行
server-inspector

# 指定报告输出目录
server-inspector --output-dir /path/to/reports

# 使用自定义硬件配置
server-inspector --profile custom_profiles.json

# 查看帮助
server-inspector --help
```

> **💡 提示**：安装后每次打开终端可直接使用 `server-inspector`，无需重新配置。

## 使用指南

### 命令行选项

```
--output-dir DIR       报告保存目录（默认：当前目录）
--profile FILE         profiles.json配置文件路径（默认：脚本同目录下的profiles.json）
--help                 显示帮助信息并退出
```

### 输出文件

每次运行生成三个带有唯一时间戳的文件：

1. **Markdown报告** (`report_<id>_<timestamp>.md`)
   - 易读的纯文本格式，包含表格、分段组织
   - 适合：文档整理、团队分享、快速查看

2. **HTML报告** (`report_<id>_<timestamp>.html`)
   - 可视化格式，具有样式和良好的排版
   - 完全独立的文件（无外部依赖）
   - 适合：邮件分享、网页查看、演示展示

3. **JSON报告** (`report_<id>_<timestamp>.json`)
   - 机器可解析的结构化数据
   - 适合：监控系统集成、自动化处理、指标收集

### 报告内容

每份报告包含：

- **系统概览**：主机名、操作系统、内核版本、唯一服务器ID
- **CPU信息**：架构、厂商、型号、核心数、频率、缓存
- **内存**：总容量、内存布局、NUMA节点信息
- **存储**：磁盘设备、容量、类型（SSD或HDD）
- **网络**：网卡数量、类型、速率能力
- **加速器**：检测到的GPU/加速器，包含：
  - 设备数量和类型
  - 显存规格（容量和带宽）
  - 性能指标（TFLOPS、INT8 TOPS）
  - 功耗信息
  - PCIe版本和连接方式
  - 推荐的推理框架
- **驱动程序**：检测到的加速器驱动版本
- **软件栈**：检测到的容器运行时、Kubernetes、通信库、ML框架
- **性能场景**：预估的推理吞吐量（tokens/秒），包括：
  - 3个代表性大语言模型（fp16和int4量化）
  - 单卡部署
  - 单节点8卡部署
  - 多节点16卡部署（分布式推理）
  - 估计的token延迟范围

## 报告解读

### 硬件规格指标

**CPU厂商识别**：通过CPUID厂商ID（x86）和CPU型号名称模式匹配来准确识别厂商。

**加速器显存**：显存容量和带宽对以下指标至关重要：
- 批处理大小限制（显存容量 / 模型显存需求）
- 推理吞吐量（通常受限于显存带宽，对于内存密集型LLM推理尤为如此）

**性能指标**：
- **TFLOPS (BF16)**：16位浮点运算的峰值吞吐量
- **TOPS (INT8)**：量化模型推理的整数运算吞吐量
- **显存带宽**：显存带宽（TB/s）- 通常是LLM推理的瓶颈

### 性能场景解读

**单卡场景**：适合对延迟敏感的应用（交互式聊天机器人）
- 1张卡 × 模型吞吐量
- 最低的首token延迟（TTFT）
- 较低的并发数

**单节点8卡**：吞吐量和延迟的平衡方案
- 8张卡配合张量并行（TP=8）
- 中等的扩展效率
- 适合批量推理和微调

**多节点16卡**：最大吞吐量
- 分布式部署：2个节点 × 8张卡
- 需要RDMA/高速网络支持
- 适合高吞吐量批量推理
- 可能因节点间通信而有更高的延迟

**性能范围**：
- 每个模型显示低、中、高三个估计值
- 低：保守估计（理论峰值的60%）
- 中：现实估计（理论峰值的70%）
- 高：优化估计（理论峰值的80%）

## 配置与扩展

### 添加新的GPU型号

编辑 `profiles.json`，找到 `accelerators` 部分中对应的GPU厂商。在每个加速器对象的 `model_specs` 数组中添加新条目：

```json
{
  "name": "GPU型号名称",
  "match_keywords": ["匹配", "产品名称", "中的", "关键词"],
  "bf16_tflops": 312,
  "int8_tops": 312,
  "hbm_gb": 80,
  "hbm_bw_tbps": 2.0,
  "tdp_w": 700,
  "pcie": "PCIe 5.0 x16",
  "interconnect": "NVLink 5"
}
```

工具将自动匹配新GPU名称与 `match_keywords` 中的关键词，并使用这些规格参数。

### 添加新的加速器类型

要支持新的加速器（如新厂商）：

1. 在 `accelerators` 数组中添加条目，包含：
   - `id`：唯一标识符
   - `detect_smi`：主检测命令
   - `detect_smi_alt`：备用检测方法
   - `queries`：提取信息的命令模式
   - `model_specs`：支持的型号数组
   - `frameworks`：推荐的推理框架

2. 如果新加速器的工具在非标准目录，更新 `bin_search_paths`

3. 如果需要新的命令类型，更新 `bin_categories`

模块化设计确保在扩展硬件支持时，inspector.py代码无需修改。

### 理解profiles.json结构

```json
{
  "schema_version": "1.0",
  "tool_version": "1.2.0",
  "bin_search_paths": [...],           // 扫描诊断工具的目录列表
  "bin_categories": {...},              // 工具类别和命令名称映射
  "cpu_vendor_x86": {...},              // x86 CPUID厂商ID映射
  "cpu_model_keywords": [...],          // 非x86 CPU的型号名称模式匹配
  "accelerators": [...],                // GPU/加速器定义
  "models_2026": [...],                 // 大语言模型规格
  "scenario_templates": [...]           // 部署场景模板定义
}
```

## 系统要求与限制

### 系统要求

- **Linux操作系统**（已在Ubuntu 20.04+、CentOS 8+、Debian 11+上测试）
- **Python 3.6及以上**（仅使用标准库）
- **无需root/sudo权限**（大多数命令以普通用户运行）
  - 某些诊断命令可能需要提升权限以获取完整信息

### 限制条件

1. **仅限Linux**：使用Linux特定的/proc、/sys和命令行工具
2. **命令可用性**：准确性取决于硬件诊断工具的安装情况：
   - 无nvidia-smi：NVIDIA检测回退到lspci（信息有限）
   - 无rocm-smi：AMD检测回退到lspci
   - 其他厂商类似

3. **环境特定性**：某些信息需要特定的驱动/工具：
   - RDMA库检测需要安装OFED/OpenFabrics
   - 容器检测需要安装Docker/containerd
   - Kubernetes检测需要kubectl

4. **性能估计**：场景性能数据是基于以下因素的估计值：
   - 峰值TFLOPS/带宽
   - 理论模型显存需求
   - 典型推理开销（未在当前系统上进行分析）
   - 实际性能因模型、框架和优化而异

## 使用示例

### 典型报告输出结构（Markdown格式）

```markdown
# 服务器评估报告

## 系统概览
- 主机名：server-a
- 服务器ID：6c5a91e2
- 操作系统：Linux (Ubuntu 22.04)
- 内核版本：5.15.0-56-generic

## CPU
- 厂商：Intel
- 型号：Xeon(R) Platinum 8380
- 核心数：112 (56P + 56E)
- 基础/睿频：2.30 / 3.40 GHz

## 内存
- 总容量：1024 GiB
- NUMA节点：8个

## 加速器
| 类型 | 数量 | 型号 | 显存 | 带宽 | TFLOPS |
|------|------|------|------|------|--------|
| NVIDIA | 8 | H100 | 80GB | 3.35 TB/s | 1979 |

## 性能场景
### 单卡推理
- 模型：Qwen 3.6B (fp16)
- 吞吐量：150-200 tokens/秒
- 首token延迟：20-30ms
- 最大并发：4-8

...
```

## 贡献

欢迎贡献代码！增强方向包括：

1. **新硬件支持**：为新兴加速器添加配置
2. **新模型支持**：扩展 `models_2026` 数组，添加更多大语言模型规格
3. **更好的性能估计**：改进场景建模算法
4. **框架支持**：添加更多ML框架的检测
5. **文档完善**：示例、故障排除指南、使用案例等

## 许可证

本项目作为硬件评估和容量规划工具提供。

## 故障排查

### 无输出或报告为空

1. 检查Python版本：`python3 --version`（需要3.6+）
2. 确保 `profiles.json` 在 `inspector.py` 同目录
3. 验证输出目录可写：`ls -ld ./reports`

### 加速器信息缺失

- 安装对应的诊断工具（nvidia-smi、rocm-smi、xpu-smi等）
- 手动检查 `/proc/devices` 和 `lspci` 验证硬件
- 验证驱动安装（如工具可用）

### 硬件检测不准确

- 某些命令可能需要特定的语言设置来正确解析
- 工具自动设置LANG=C以保证一致的输出
- 手动运行命令检查：`nvidia-smi -q` 或 `rocm-smi --showproductname`

## 支持与反馈

如有问题、疑问或想提交贡献，请在GitHub上开issue。
