# Server Inspector

A universal hardware capacity assessment tool for evaluating server capabilities for LLM inference deployment. Automatically detects and profiles hardware including CPUs, memory, accelerators, storage, network, and software ecosystem.

## Features

- **Multi-Accelerator Support**: Detects 11+ GPU/accelerator types (NVIDIA, AMD, Hygon, Kunlun, Ascend, Iluvatar, Cambricon, Biren, MetaX, MoorThreads, Habana)
- **Multi-Architecture CPU Recognition**: Supports Intel x86, AMD, ARM-based (Kunpeng, Phytium), RISC-V, LoongArch, Sunway, and Power architectures
- **30+ GPU Model Specifications**: Pre-configured models with performance (TFLOPS), memory (HBM), bandwidth, and power consumption metrics
- **Intelligent Command Discovery**: Auto-detects hardware diagnostic tools (nvidia-smi, rocm-smi, xpu-smi, etc.) in system PATH and common installation directories
- **Performance Scenario Modeling**: Estimates inference performance across single-card, single-node (8-card), and multi-node (16-card) deployment scenarios
- **Dynamic Profile Configuration**: Hardware knowledge stored in external `profiles.json` - easily extend for new accelerators and GPUs without code modification
- **Multiple Output Formats**: Generates Markdown reports (human-readable), HTML (visual), and JSON (machine-parseable) for integration with monitoring systems
- **Container & Kubernetes Detection**: Identifies Docker, containerd, Kubernetes, GPU container toolkits (NVIDIA, AMD, Intel), and scheduling frameworks
- **RDMA & Cluster Toolkit Detection**: Scans for multi-node communication libraries and frameworks
- **ML Framework Detection**: Identifies PyTorch, TensorFlow, JAX, and other inference-related libraries in system

## Supported Hardware

### GPUs & Accelerators
- **NVIDIA**: Full CUDA device support via nvidia-smi
- **AMD**: RDNA/CDNA cards via rocm-smi
- **Hygon**: DCU accelerators (dcmi tool)
- **Kunlun**: XTCPU/XTCPU-C models via xpu-smi
- **Ascend**: Huawei NPU via npu-smi
- **Iluvatar**: Dedicated GPUs via iluvatar-smi
- **Cambricon**: MLU accelerators via cnmon
- **Biren**: SR series via biren-smi
- **MetaX**: MPS cores via metax-smi
- **MoorThreads**: MTT series via mtm-smi
- **Habana**: Gaudi accelerators via hl-smi

### CPUs
- Intel (Xeon, Core)
- AMD (EPYC, Ryzen)
- Arm-based (Kunpeng, Phytium, Ampere)
- RISC-V based processors
- LoongArch processors
- Sunway processors
- IBM Power processors

## Installation

### Prerequisites
- Linux-only (kernel detection ensures safe operation)
- Python 3.6 or later
- pip

### Setup

```bash
# Clone the repository
git clone https://github.com/b-birdy/server-inspector.git
cd server-inspector

# No external Python dependencies required
# (Uses only Python standard library: subprocess, json, re, platform, etc.)

# Run the tool
python3 inspector.py --output-dir ./reports --profile profiles.json
```

## Quick Start

```bash
# Basic usage - generates reports in current directory
python3 inspector.py

# Specify output directory
python3 inspector.py --output-dir /path/to/reports

# Use custom hardware profile
python3 inspector.py --profile custom_profiles.json --output-dir ./reports

# View help
python3 inspector.py --help
```

## Usage Guide

### Command-Line Options

```
--output-dir DIR       Directory to save reports (default: current directory)
--profile FILE         Path to profiles.json configuration file (default: profiles.json in script directory)
--help                 Show help message and exit
```

### Output Files

Each run generates three files with a unique timestamp:

1. **Markdown Report** (`report_<id>_<timestamp>.md`)
   - Human-readable format suitable for documentation
   - Includes tables, sections, and easy interpretation
   - Best for: sharing with team, documentation, quick review

2. **HTML Report** (`report_<id>_<timestamp>.html`)
   - Visual format with styling and organization
   - Fully self-contained (no external dependencies)
   - Best for: sharing via email, web viewing, presentations

3. **JSON Report** (`report_<id>_<timestamp>.json`)
   - Machine-parseable structured data
   - Best for: integration with monitoring systems, automation, metrics collection

### Report Contents

Each report includes:

- **System Overview**: Hostname, OS, kernel version, unique server ID
- **CPU Information**: Architecture, vendor, model, core count, frequencies, cache
- **Memory**: Total RAM, memory layout, node information
- **Storage**: Disk devices, capacity, types (SSD vs HDD)
- **Network**: Interface count, types, speed capabilities
- **Accelerators**: Detected GPUs/accelerators with:
  - Device count and type
  - Memory specifications (VRAM size and bandwidth)
  - Performance metrics (TFLOPS, INT8 TOPS)
  - Power consumption
  - PCIe generation and connectivity
  - Recommended inference frameworks
- **Drivers**: Version information for detected accelerator drivers
- **Software Stack**: Detected container runtimes, Kubernetes, communication libraries, ML frameworks
- **Performance Scenarios**: Predicted inference throughput (tokens/second) for:
  - 3 representative LLM models (fp16 and int4 quantized)
  - Single-card deployment
  - Single-node 8-card deployment
  - Multi-node 16-card deployment (distributed inference)
  - Estimated token latency ranges

## Interpreting Reports

### Hardware Specifications

**CPU Vendor Detection**: Uses CPUID vendor ID (x86) and CPU model name pattern matching for proper vendor classification.

**Accelerator Memory**: VRAM size and bandwidth are critical for:
- Batch size limits (HBM capacity / model memory requirement)
- Inference throughput (limited by HBM bandwidth for memory-bound operations)

**Performance Metrics**:
- **TFLOPS (BF16)**: Peak floating-point throughput for 16-bit operations
- **TOPS (INT8)**: Integer throughput for quantized model inference
- **HBM Bandwidth**: Memory bandwidth (TB/s) - often bottleneck for LLM inference

### Performance Scenario Interpretation

**Single-Card Scenario**: Best-case for latency-sensitive applications (interactive chatbots)
- 1 card × model throughput
- Best token-to-first-token (TTFT) latency
- Lower concurrency

**Single-Node 8-Card**: Balance of throughput and latency
- 8 cards with tensor parallelism (TP=8)
- Moderate scaling efficiency
- Suitable for batch inference and fine-tuning

**Multi-Node 16-Card**: Maximum throughput
- Distributed training across 2 nodes × 8 cards
- Requires RDMA/high-speed network
- Best for high-throughput batch inference
- May have higher latency due to inter-node communication

**Performance Ranges**:
- Each model shows low/mid/high estimates
- Low: conservative estimate (60% of theoretical peak)
- Mid: realistic estimate (70% of theoretical peak)  
- High: optimized estimate (80% of theoretical peak)

## Configuration & Extension

### Adding New GPU Models

Edit `profiles.json` and locate the `accelerators` section for your GPU vendor. For each accelerator object, find the `model_specs` array and add a new entry:

```json
{
  "name": "GPU-Model-Name",
  "match_keywords": ["matching", "strings", "from", "product", "name"],
  "bf16_tflops": 312,
  "int8_tops": 312,
  "hbm_gb": 80,
  "hbm_bw_tbps": 2.0,
  "tdp_w": 700,
  "pcie": "PCIe 5.0 x16",
  "interconnect": "NVLink 5"
}
```

The tool will match new GPU names against `match_keywords` and use these specifications.

### Adding New Accelerator Types

To add support for a new accelerator (e.g., new vendor):

1. Add entry to `accelerators` array with:
   - `id`: unique identifier
   - `detect_smi`: primary detection command
   - `detect_smi_alt`: alternative detection method
   - `queries`: command patterns for extracting information
   - `model_specs`: array of supported models
   - `frameworks`: recommended inference frameworks

2. Update `bin_search_paths` if the new accelerator has tools in non-standard directories

3. Add category to `bin_categories` if new command types are needed

The modular design means inspector.py code remains unchanged when extending hardware support.

### Understanding profiles.json Structure

```json
{
  "schema_version": "1.0",
  "tool_version": "1.2.0",
  "bin_search_paths": [...],           // Directories to scan for diagnostic tools
  "bin_categories": {...},              // Tool categories and command names
  "cpu_vendor_x86": {...},              // x86 CPUID vendor ID mappings
  "cpu_model_keywords": [...],          // Pattern matching for non-x86 CPUs
  "accelerators": [...],                // GPU/accelerator definitions
  "models_2026": [...],                 // LLM model specifications
  "scenario_templates": [...]           // Deployment scenario definitions
}
```

## Requirements & Limitations

### System Requirements

- **Linux** (tested on Ubuntu 20.04+, CentOS 8+, Debian 11+)
- **Python 3.6+** (uses only standard library)
- **Root/sudo access** not required (most commands run as regular user)
  - Some diagnostic commands may output limited info without elevated privileges

### Limitations

1. **Linux-only**: Uses Linux-specific /proc, /sys, and command-line tools
2. **Command Availability**: Accuracy depends on hardware diagnostic tools being installed:
   - Without `nvidia-smi`: NVIDIA detection falls back to lspci (limited info)
   - Without `rocm-smi`: AMD detection falls back to lspci
   - Similar fallbacks for other vendors

3. **Environment-Specific**: Some information requires specific drivers/tools:
   - RDMA library detection requires installed OFED/OpenFabrics
   - Container detection requires Docker/containerd to be installed
   - Kubernetes detection requires kubectl

4. **Performance Estimation**: Scenario performance numbers are estimates based on:
   - Peak TFLOPS / bandwidth
   - Theoretical model memory requirements
   - Typical inference overhead (not profiled on this system)
   - Actual performance varies by model, framework, and optimization

## Examples

### Typical Report Output Structure (Markdown)

```markdown
# Server Assessment Report

## System Overview
- Hostname: server-a
- Server ID: 6c5a91e2
- OS: Linux (Ubuntu 22.04)
- Kernel: 5.15.0-56-generic

## CPU
- Vendor: Intel
- Model: Xeon(R) Platinum 8380
- Cores: 112 (56P + 56E)
- Base/Turbo: 2.30 / 3.40 GHz

## Memory
- Total: 1024 GiB
- Nodes: 8 (NUMA)

## Accelerators
| Type | Count | Model | VRAM | Bandwidth | TFLOPS |
|------|-------|-------|------|-----------|--------|
| NVIDIA | 8 | H100 | 80GB | 3.35 TB/s | 1979 |

## Performance Scenarios
### Single Card Inference
- Model: Qwen 3.6B (fp16)
- Throughput: 150-200 tokens/sec
- TTFT: 20-30ms
- Max Concurrency: 4-8

...
```

## Contributing

Contributions welcome! Areas for enhancement:

1. **New Hardware Support**: Add profiles for emerging accelerators
2. **New Models**: Extend `models_2026` array with additional LLM specifications
3. **Better Performance Estimation**: Improved algorithms for scenario modeling
4. **Framework Support**: Add detection for additional ML frameworks
5. **Documentation**: Examples, troubleshooting guides, use cases

## License

This project is provided as-is for hardware assessment and capacity planning purposes.

## Troubleshooting

### No Output or Empty Reports

1. Check Python version: `python3 --version` (requires 3.6+)
2. Ensure `profiles.json` exists in same directory as `inspector.py`
3. Verify output directory is writable: `ls -ld ./reports`

### Missing Accelerator Information

- Install corresponding diagnostic tools (nvidia-smi, rocm-smi, xpu-smi, etc.)
- Check `/proc/devices` and `lspci` manually to verify hardware presence
- Verify driver installation if tools are available

### Incorrect Hardware Detection

- Some commands may require specific locales to parse correctly
- Tool automatically sets LANG=C for consistent output
- Check individual command output: `nvidia-smi -q` or `rocm-smi --showproductname`

## Support & Feedback

For issues, questions, or contributions, please open an issue on GitHub.
