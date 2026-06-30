#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务器推理能力评估工具 v1.2.0
Server Inference Capability Inspector

v1.2.0 更新:
  - 引入外部配置文件 profiles.json，所有"硬件相关的命令 / 解析 / 规格"统一由配置驱动
  - 算力卡检测从硬编码变为通用循环（增加新卡只需改 profiles.json）
  - 支持卡型号识别 → 自动匹配规格（NVIDIA H100/H200/B200 / AMD MI300X / 昇腾 910C / Gaudi3 等）
  - 性能预估场景模块化，所有加速卡通用
  - CPU 厂商识别扩展（Intel/AMD/海光/兆芯/鲲鹏/飞腾/龙芯/申威/Power/RISC-V）
  - lscpu 强制英文 locale（修复中文系统乱码）
  - 命令扫描自动包含 $PATH
  - 平台 guard（非 Linux 提前退出）

用法: python3 inspector.py [--output-dir ./reports] [--profile profiles.json]
"""

import subprocess
import os
import re
import sys
import json
import zlib
import base64
import html as html_lib
import datetime
import socket
import argparse
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional


# ─────────────────────────────────────────────
# 平台 guard
# ─────────────────────────────────────────────
# --encode-profile 是纯文件工具，不依赖 Linux 接口，跳过平台检查
if sys.platform not in ("linux", "linux2") and "--encode-profile" not in sys.argv:
    print(f"⚠️  本工具仅支持 Linux（当前平台: {sys.platform}）", file=sys.stderr)
    print("    Windows / macOS 上无法采集 Linux 专属硬件信息，请在目标 Linux 服务器上运行。", file=sys.stderr)
    sys.exit(1)


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

_PCIE_CLASS_RE = re.compile(r"\[([0-9a-fA-F]{4})\]:")


def _is_accel_pcie_line(line: str) -> bool:
    """PCIe class 白名单：只放行可能是加速卡 / GPU 的设备类。

    避免把 AMD/Intel CPU 自带的 Host Bridge / IOMMU / PSP / USB / SATA
    等设备误判成加速卡（曾把 AMD EPYC 自带 ~149 个 PCIe 桥接器识别成 149 张 ROCm GPU）。

    - 03xx: Display controller (含 VGA / 3D / Other display)
    - 12xx: Processing accelerators
    - 0b40: Coprocessor
    - 1180: Signal processing
    """
    m = _PCIE_CLASS_RE.search(line)
    if not m:
        return True
    cls = m.group(1).lower()
    return cls.startswith(("03", "12")) or cls in ("0b40", "1180")


def run(cmd: str, timeout: int = 30) -> Tuple[int, str, str]:
    try:
        env = os.environ.copy()
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, errors="replace", env=env)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"[超时] {cmd}"
    except Exception as e:
        return -1, "", str(e)


def progress(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"\033[36m[{ts}]\033[0m {msg}", flush=True)


# ─────────────────────────────────────────────
# 配置文件加载
# ─────────────────────────────────────────────

# 用于配置文件弱编码的内部密钥（仅用于阻止"打开就能读"，非加密保护）
_PROFILE_KEY = b"svi-internal-2026-pkg-v1"


def _profile_xor(data: bytes) -> bytes:
    return bytes(b ^ _PROFILE_KEY[i % len(_PROFILE_KEY)] for i, b in enumerate(data))


def encode_profile_bytes(plain: bytes) -> bytes:
    """明文 JSON 字节 → 编码后字节（zlib 压缩 + XOR + Base64）。"""
    return base64.b64encode(_profile_xor(zlib.compress(plain, level=9)))


def decode_profile_bytes(enc: bytes) -> bytes:
    """编码后字节 → 明文 JSON 字节。"""
    return zlib.decompress(_profile_xor(base64.b64decode(enc)))


def _resolve_profile_path(path: str) -> Optional[Path]:
    """按优先级查找 profile 文件：先 .enc，再 .json，最后原样路径。"""
    p = Path(path)
    bases = [p.parent] if p.is_absolute() else [Path.cwd(), Path(__file__).parent]
    stems = [p.stem] if p.is_absolute() else [Path(path).stem]
    candidates: List[Path] = []
    for base in bases:
        for stem in stems:
            candidates.append(base / f"{stem}.enc")
            candidates.append(base / f"{stem}.json")
            candidates.append(base / Path(path).name)
    if p.is_absolute():
        candidates.insert(0, p)
    return next((c for c in candidates if c.exists()), None)


def write_contribution_file(hostname: str, hw: Dict, sw: Dict,
                            profile: Dict, unknown_items: List[Dict]) -> Optional[Path]:
    """检测到未收录硬件时，把结构化数据写到 ~/.server-inspector/contribute/<ts>_<host>.json。

    不含敏感信息（用户名/密码/公网 IP/绝对路径）。返回文件路径，无则 None。
    """
    if not unknown_items:
        return None
    contrib_dir = Path.home() / ".server-inspector" / "contribute"
    try:
        contrib_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"⚠️  贡献目录创建失败 ({contrib_dir}): {e}", file=sys.stderr)
        return None

    now = datetime.datetime.now()
    ts = now.strftime("%Y%m%d-%H%M%S")
    h_short = (hostname or "unknown").split(".")[0] or "unknown"
    out_path = contrib_dir / f"{ts}_{h_short}.json"

    accel = hw.get("算力卡", {})
    cpu = hw.get("CPU", {})
    os_info = sw.get("操作系统", {})
    drv = sw.get("驱动程序", {})

    payload = {
        "schema_version":   "1.0",
        "submit_time":      now.strftime("%Y-%m-%d %H:%M:%S"),
        "tool_version":     profile.get("tool_version", "?"),
        "hostname":         h_short,
        "os":               os_info.get("发行版", ""),
        "kernel":           os_info.get("内核版本", ""),
        "unknown_items":    unknown_items,
        "raw_data": {
            "cpu_summary": {
                k: cpu.get(k, "")
                for k in ["厂商", "型号", "架构", "物理CPU数", "每CPU核数",
                          "总线程数", "最大主频", "向量指令集", "NUMA节点数",
                          "虚拟化"]
            },
            "lspci_nn":     run("lspci -nn 2>/dev/null", timeout=15)[1],
            "lscpu":        run("lscpu 2>/dev/null", timeout=15)[1],
            "accel_summary": {
                "detected_types":    accel.get("_检测到的卡型", []),
                "no_accelerator":    bool(accel.get("_无算力卡", False)),
                "other_pcie":        accel.get("其他PCIe加速设备", ""),
                "detected_cards": [
                    {
                        "display_name":     c.get("_display_name", ""),
                        "vendor":           c.get("_vendor", ""),
                        "accel_id":         c.get("_accel_id", ""),
                        "model_name":       c.get("_model_name", ""),
                        "card_count":       c.get("_card_count", 0),
                        "single_vram_mib":  c.get("_single_vram_mib", 0),
                        "pcie_devices":     c.get("PCIe设备列表", ""),
                        "smi_outputs":      c.get("命令摘要", {}),
                    }
                    for c in accel.get("_detected_cards", [])
                ],
            },
            "drivers": {
                k: v for k, v in drv.items()
                if isinstance(v, (str, int, float))
            },
        },
    }

    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def load_profile(path: str) -> Dict:
    """加载 profile。优先加密 .enc，回退到明文 .json。"""
    chosen = _resolve_profile_path(path)
    if not chosen:
        print(f"❌ 找不到配置文件: {path}", file=sys.stderr)
        sys.exit(2)
    try:
        raw = chosen.read_bytes()
        if chosen.suffix == ".enc":
            raw = decode_profile_bytes(raw)
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, zlib.error, ValueError, base64.binascii.Error) as e:
        print(f"❌ 配置文件解析失败 ({chosen}): {e}", file=sys.stderr)
        sys.exit(2)


# ─────────────────────────────────────────────
# 命令注册表（配置驱动 + $PATH 扫描）
# ─────────────────────────────────────────────

class CommandRegistry:
    def __init__(self, profile: Dict):
        self.categories = profile.get("bin_categories", {})
        self.search_paths = list(profile.get("bin_search_paths", []))
        # 加入 $PATH
        for p in os.environ.get("PATH", "").split(os.pathsep):
            if p and p not in self.search_paths:
                self.search_paths.append(p)
        self.available: Dict[str, List[str]] = {c: [] for c in self.categories}
        self.paths: Dict[str, str] = {}
        self._scan()

    def _scan(self):
        for cat, cmds in self.categories.items():
            for cmd in cmds:
                for d in self.search_paths:
                    p = os.path.join(d, cmd)
                    if os.path.exists(p) and os.access(p, os.X_OK):
                        if cmd not in self.available[cat]:
                            self.available[cat].append(cmd)
                            self.paths[cmd] = p
                        break

    def has(self, cmd: str) -> bool:
        return cmd in self.paths

    def list(self, category: str) -> List[str]:
        return self.available.get(category, [])

    def summary(self) -> Dict[str, int]:
        return {c: len(v) for c, v in self.available.items() if v}


# ─────────────────────────────────────────────
# 硬件采集
# ─────────────────────────────────────────────

class HardwareCollector:
    def __init__(self, profile: Dict, registry: CommandRegistry):
        self.profile = profile
        self.reg = registry

    def collect_cpu(self) -> Dict:
        info: Dict[str, Any] = {}
        _, out, _ = run("lscpu 2>/dev/null")
        lscpu: Dict[str, str] = {}
        for line in out.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                lscpu[k.strip()] = v.strip()

        info["架构"]      = lscpu.get("Architecture", "未知")
        info["型号"]      = lscpu.get("Model name", "未知")
        info["物理CPU数"] = lscpu.get("Socket(s)", "未知")
        info["每CPU核数"] = lscpu.get("Core(s) per socket", "未知")
        info["总线程数"]  = lscpu.get("CPU(s)", "未知")
        info["超线程"]    = "启用" if lscpu.get("Thread(s) per core", "1") != "1" else "禁用"

        # CPU 频率（多 fallback）
        cur_mhz = lscpu.get("CPU MHz", "")
        if not cur_mhz:
            _, cpuinfo, _ = run("grep -m1 'cpu MHz' /proc/cpuinfo 2>/dev/null")
            if ":" in cpuinfo:
                cur_mhz = cpuinfo.split(":")[-1].strip()
        info["当前主频"] = f"{cur_mhz} MHz" if cur_mhz else "未知"

        max_mhz = lscpu.get("CPU max MHz", "")
        if max_mhz:
            try:
                info["最大主频"] = f"{float(max_mhz):.0f} MHz"
            except ValueError:
                info["最大主频"] = max_mhz
        else:
            info["最大主频"] = "未知"

        info["NUMA节点数"] = lscpu.get("NUMA node(s)", "未知")
        info["L1d缓存"]  = lscpu.get("L1d cache", "未知")
        info["L1i缓存"]  = lscpu.get("L1i cache", "未知")
        info["L2缓存"]   = lscpu.get("L2 cache", "未知")
        info["L3缓存"]   = lscpu.get("L3 cache", "未知")
        info["虚拟化"]   = lscpu.get("Virtualization", lscpu.get("Hypervisor vendor", "未启用"))

        # 向量指令集
        _, flags_out, _ = run("grep -m1 'flags\\|Features' /proc/cpuinfo 2>/dev/null")
        flags = flags_out.split(":")[-1].split() if ":" in flags_out else []
        if "avx512f" in flags:
            info["向量指令集"] = "AVX-512" + (" + AMX" if "amx_tile" in flags else "")
        elif "avx2" in flags:
            info["向量指令集"] = "AVX2"
        elif "avx" in flags:
            info["向量指令集"] = "AVX"
        elif "sve" in flags:
            info["向量指令集"] = "ARM SVE" + (" + SVE2" if "sve2" in flags else "")
        elif "asimd" in flags or "neon" in flags:
            info["向量指令集"] = "ARM NEON"
        elif "rvv" in flags:
            info["向量指令集"] = "RISC-V Vector"
        else:
            info["向量指令集"] = "未识别"

        # 厂商识别（profile 驱动）
        _, vendor_line, _ = run("grep -m1 'vendor_id\\|CPU implementer' /proc/cpuinfo 2>/dev/null")
        vendor = vendor_line.split(":")[-1].strip() if ":" in vendor_line else ""
        info["厂商"] = self._identify_cpu_vendor(vendor, info["架构"], info["型号"])

        if self.reg.has("numactl"):
            _, numa_out, _ = run("numactl --hardware 2>/dev/null | head -30")
            if numa_out:
                info["NUMA拓扑"] = numa_out

        return info

    def _identify_cpu_vendor(self, vendor_id: str, arch: str, model: str) -> str:
        # 1. x86 vendor_id 直接匹配
        x86_map = self.profile.get("cpu_vendor_x86", {})
        for k, v in x86_map.items():
            if k.strip() and k.strip() in vendor_id:
                return v
        # 2. 模型关键字匹配（ARM/LoongArch/RISC-V/Power 等）
        arch_lower = arch.lower()
        for entry in self.profile.get("cpu_model_keywords", []):
            if entry["match"] in model:
                return entry["vendor"]
        # 3. 架构兜底
        if "aarch64" in arch_lower or "arm" in arch_lower:
            return "ARM 架构（未识别 SoC）"
        if "loong" in arch_lower:
            return "龙芯 (LoongArch)"
        if "riscv" in arch_lower:
            return "RISC-V（未识别 SoC）"
        if "ppc" in arch_lower or "power" in arch_lower:
            return "IBM Power"
        return vendor_id or "未知"

    def collect_memory(self) -> Dict:
        info: Dict[str, Any] = {}
        _, free_out, _ = run("free -h 2>/dev/null")
        for line in free_out.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                info["总容量"]  = parts[1] if len(parts) > 1 else "未知"
                info["已使用"]  = parts[2] if len(parts) > 2 else "未知"
                info["可用"]    = parts[6] if len(parts) > 6 else "未知"
            elif line.startswith("Swap:"):
                parts = line.split()
                info["Swap总量"] = parts[1] if len(parts) > 1 else "未知"

        _, mem_info, _ = run(
            "grep -E '^(MemTotal|HugePages_Total|Hugepagesize)' /proc/meminfo 2>/dev/null"
        )
        for line in mem_info.splitlines():
            k, _, v = line.partition(":")
            info[k.strip()] = v.strip()

        if self.reg.has("dmidecode"):
            _, dmi_raw, _ = run("dmidecode -t 17 2>/dev/null", timeout=45)
            if dmi_raw and "Memory Device" in dmi_raw:
                cnt = dmi_raw.count("Memory Device")
                installed = len([
                    l for l in dmi_raw.splitlines()
                    if re.search(r"^\s*Size:", l) and "No Module" not in l
                ])
                speeds = [
                    l.split(":")[-1].strip()
                    for l in dmi_raw.splitlines()
                    if re.search(r"^\s*Speed:", l)
                    and "Unknown" not in l and "Configured" not in l
                ]
                types = [
                    l.split(":")[-1].strip()
                    for l in dmi_raw.splitlines()
                    if re.search(r"^\s*Type:", l)
                    and "Unknown" not in l and "Detail" not in l
                    and "Form Factor" not in l
                ]
                mfrs = [
                    l.split(":")[-1].strip()
                    for l in dmi_raw.splitlines()
                    if re.search(r"^\s*Manufacturer:", l)
                    and l.split(":")[-1].strip() not in ("", "Unknown", "Not Specified")
                ]
                info["内存条总插槽"] = str(cnt)
                info["内存条已使用"] = str(installed)
                info["内存类型"] = types[0] if types else "未知"
                info["内存频率"] = speeds[0] if speeds else "未知"
                info["内存厂商"] = mfrs[0] if mfrs else "未知"
            else:
                info["内存条详情"] = "需要 root 权限才能读取 (dmidecode)"
        return info

    def collect_storage(self) -> Dict:
        info: Dict[str, Any] = {}
        _, lsblk_out, _ = run(
            "lsblk -o NAME,TYPE,SIZE,ROTA,MODEL,TRAN,MOUNTPOINT 2>/dev/null | "
            "grep -vE '^loop|^sr'"
        )
        info["块设备列表"] = lsblk_out or "无法获取"

        _, df_out, _ = run(
            "df -hT 2>/dev/null | grep -v -E 'tmpfs|devtmpfs|overlay|udev|^Filesystem'"
        )
        info["文件系统挂载"] = df_out or "无法获取"

        if self.reg.has("nvme"):
            _, nvme_out, _ = run("nvme list 2>/dev/null")
            if nvme_out and "Node" in nvme_out:
                info["NVMe设备"] = nvme_out

        if self.reg.has("hdparm"):
            _, disks_out, _ = run(
                "lsblk -d -o NAME,TYPE 2>/dev/null | grep disk | awk '{print $1}' | head -3"
            )
            perf: Dict[str, str] = {}
            if disks_out:
                for disk in disks_out.split("\n")[:2]:
                    disk = disk.strip()
                    if disk:
                        _, hd, _ = run(f"hdparm -t /dev/{disk} 2>/dev/null | tail -2")
                        if hd and "Timing" in hd:
                            perf[f"/dev/{disk}"] = hd.strip()
            if perf:
                info["磁盘读速参考"] = perf

        for fs_type, label in [
            ("nfs", "NFS"), ("ceph", "Ceph"),
            ("lustre", "Lustre"), ("glusterfs", "GlusterFS"),
            ("beegfs", "BeeGFS"), ("gpfs", "GPFS"),
        ]:
            _, mnt, _ = run(f"mount 2>/dev/null | grep -i 'type {fs_type}'")
            if mnt:
                info[f"共享存储_{label}"] = mnt

        if self.reg.has("multipath"):
            _, mp_out, _ = run("multipath -ll 2>/dev/null | head -40")
            if mp_out:
                info["多路径存储"] = mp_out

        _, mdstat, _ = run("cat /proc/mdstat 2>/dev/null")
        if mdstat and "Personalities" in mdstat and "unused devices" not in mdstat.split("\n")[-1]:
            info["软件RAID"] = mdstat

        if self.reg.has("smartctl"):
            _, disks, _ = run("lsblk -d -o NAME,TYPE 2>/dev/null | awk '$2==\"disk\"{print $1}' | head -2")
            smart: Dict[str, str] = {}
            for d in disks.split("\n"):
                d = d.strip()
                if d:
                    _, s, _ = run(f"smartctl -H /dev/{d} 2>/dev/null | grep -i 'health\\|PASSED\\|FAILED' | head -2")
                    if s:
                        smart[f"/dev/{d}"] = s.replace("\n", " ")
            if smart:
                info["SMART健康"] = smart

        return info

    def collect_network(self) -> Dict:
        info: Dict[str, Any] = {}
        _, ip_out, _ = run("ip -br addr show 2>/dev/null")
        info["网络接口概览"] = ip_out or "无法获取"

        _, ifaces_out, _ = run(
            "ip link show 2>/dev/null | grep '^[0-9]' | awk '{print $2}' | sed 's/://'"
        )
        eth_details: Dict[str, Dict[str, str]] = {}
        if ifaces_out:
            for iface in ifaces_out.split("\n"):
                iface = iface.strip()
                if iface and iface != "lo":
                    _, et, _ = run(
                        f"ethtool {iface} 2>/dev/null | grep -E 'Speed|Duplex|Link detected|Port'"
                    )
                    if et:
                        d = {}
                        for ln in et.splitlines():
                            if ":" in ln:
                                k, _, v = ln.partition(":")
                                d[k.strip()] = v.strip()
                        eth_details[iface] = d
        if eth_details:
            info["网卡速率"] = eth_details

        _, lspci_net, _ = run(
            "lspci 2>/dev/null | grep -iE 'ethernet|infiniband|mellanox|fiber|fibre channel'"
        )
        if lspci_net:
            info["PCIe网络设备"] = lspci_net

        if self.reg.has("ibstatus"):
            _, ib_out, _ = run("ibstatus 2>/dev/null")
            if ib_out:
                info["RDMA链路状态(ibstatus)"] = ib_out

        if self.reg.has("rdma"):
            _, rdma_out, _ = run("rdma link show 2>/dev/null")
            if rdma_out:
                info["RDMA链路"] = rdma_out

        if self.reg.has("ibv_devinfo"):
            _, ibv_out, _ = run("ibv_devinfo 2>/dev/null | grep -E 'hca_id|active_mtu|state|rate'")
            if ibv_out:
                info["RDMA设备详情(ibv_devinfo)"] = ibv_out

        if self.reg.has("show_gids"):
            _, gids_out, _ = run("show_gids 2>/dev/null")
            if gids_out:
                info["RoCE GID表"] = gids_out

        _, fc_sysfs, _ = run("ls /sys/class/fc_host/ 2>/dev/null")
        if fc_sysfs:
            info["FC Host设备"] = fc_sysfs
            _, fc_speed, _ = run(
                "for h in /sys/class/fc_host/host*; do "
                "echo $(basename $h): $(cat $h/speed 2>/dev/null) "
                "WWPN=$(cat $h/port_name 2>/dev/null); done"
            )
            if fc_speed:
                info["FC HBA端口速率"] = fc_speed
        return info

    # ── 算力卡：完全 profile 驱动 ──
    def collect_accelerators(self) -> Dict:
        info: Dict[str, Any] = {}
        detected_cards: List[Dict] = []
        total_vram_mib = 0
        total_cards = 0

        for accel in self.profile.get("accelerators", []):
            result = self._detect_single_accel(accel)
            if not result:
                continue
            info[accel["id"]] = result
            detected_cards.append(result)
            total_vram_mib += result.get("_total_vram_mib", 0)
            total_cards += result.get("_card_count", 0)

        if not detected_cards:
            _, vga, _ = run("lspci 2>/dev/null | grep -iE 'VGA|3D controller|AI accelerat'")
            if vga:
                info["其他PCIe加速设备"] = vga
            else:
                info["_无算力卡"] = True

        info["_检测到的卡型"]   = [c["_display_name"] for c in detected_cards]
        info["_主要算力类型"]   = " | ".join(c["_display_name"] for c in detected_cards) or "未检测到"
        info["_总卡数"]         = total_cards
        info["_总显存MiB"]      = total_vram_mib
        info["_detected_cards"] = detected_cards
        return info

    def _detect_single_accel(self, accel: Dict) -> Optional[Dict]:
        """根据 profile 中单条 accelerator 定义，尝试检测。"""
        smi_cmds = [accel.get("detect_smi"), accel.get("detect_smi_alt")]
        smi_cmds = [c for c in smi_cmds if c]
        keyword = accel.get("detect_smi_keyword", "")
        queries = accel.get("queries", {})

        smi_outputs: Dict[str, str] = {}
        has_smi = False
        for cmd in smi_cmds:
            if self.reg.has(cmd):
                has_smi = True
                break

        if has_smi:
            for qname, qcmd in queries.items():
                _, out, _ = run(qcmd, timeout=45)
                if out:
                    smi_outputs[qname] = out

        # 是否真的检测到这种卡
        detected = False
        if has_smi and smi_outputs:
            joined = "\n".join(smi_outputs.values())
            if not keyword or keyword.lower() in joined.lower() or keyword in joined:
                detected = True

        # lspci 兜底
        lspci_pat = accel.get("lspci_pattern", "")
        lspci_out = ""
        if lspci_pat:
            _, lspci_raw, _ = run("lspci -nn 2>/dev/null")
            for ln in lspci_raw.splitlines():
                if not _is_accel_pcie_line(ln):
                    continue
                if re.search(lspci_pat, ln, re.IGNORECASE):
                    lspci_out += ln + "\n"
            if lspci_out and not detected:
                detected = True

        if not detected:
            return None

        # 解析卡数 + 显存
        card_count, single_mib, model_name = self._parse_accel_specs(
            accel, smi_outputs, lspci_out
        )

        # 匹配规格
        spec = self._match_model_spec(accel, model_name, smi_outputs, lspci_out)

        # 显存策略：始终信任 smi 实测值；smi 取不到（=0）才用 spec 标称 hbm_gb 兜底。
        # 不做 sanity check 回退 —— 魔改显存（如 2060 SUPER 改 16GB）、SR-IOV
        # 切片、vGPU 等场景下 smi 报告的就是真实可用值，覆盖会破坏检测。
        if single_mib == 0:
            spec_hbm_gb = spec.get("hbm_gb", 0)
            if spec_hbm_gb:
                single_mib = int(spec_hbm_gb * 1024)

        return {
            "_display_name":    accel["display_name"],
            "_vendor":          accel["vendor"],
            "_accel_id":        accel["id"],
            "_card_count":      card_count,
            "_single_vram_mib": single_mib,
            "_total_vram_mib":  single_mib * card_count,
            "_model_name":      model_name,
            "_spec":            spec,
            "_frameworks":      accel.get("frameworks", []),
            "卡型号":           model_name or "未识别",
            "卡数":             card_count,
            "单卡显存":         f"{single_mib} MiB ({single_mib/1024:.1f} GB)" if single_mib else "未知",
            "总显存":           f"{single_mib*card_count} MiB ({single_mib*card_count/1024:.1f} GB)" if single_mib else "未知",
            "BF16 TFLOPS":      (
                "不支持" if spec.get("bf16_tflops") == 0
                else (f"{spec.get('bf16_tflops')} TFLOPS/卡"
                      if spec.get("bf16_tflops") not in (None, "")
                      else "暂无数据")
            ),
            "PCIe":             spec.get("pcie", "未知"),
            "互联":             spec.get("interconnect", "未知"),
            "TDP":              f"{spec.get('tdp_w','?')} W",
            "PCIe设备列表":     lspci_out.strip() or "未检测",
            "命令摘要":         {k: (v[:1200] if isinstance(v, str) else v)
                                for k, v in smi_outputs.items()},
        }

    def _parse_accel_specs(self, accel: Dict, outputs: Dict[str, str],
                           lspci_out: str) -> Tuple[int, int, str]:
        """从 smi 输出里解析 (卡数, 单卡显存 MiB, 卡型号)"""
        card_count = 0
        single_mib = 0
        model_name = ""

        accel_id = accel["id"]
        joined = "\n".join(outputs.values())

        # 通用 "X MiB / Y MiB" 模式（昆仑、天数、寒武纪等）
        memmatch = []
        for m in re.finditer(r"(\d+)\s*MiB\s*/\s*(\d+)\s*MiB", joined):
            total = int(m.group(2))
            if total > 100:
                memmatch.append(total)
        if memmatch:
            single_mib = memmatch[0]
            card_count = len(memmatch)

        # NVIDIA: 单独一列
        if accel_id == "nvidia":
            raw = outputs.get("mem_mib", "")
            mems = [int(x) for x in raw.splitlines() if x.strip().isdigit()]
            if mems:
                single_mib = mems[0]
                card_count = len(mems)
            csv = outputs.get("csv", "")
            if csv:
                first = csv.splitlines()[0]
                model_name = first.split(",")[0].strip()
            elif outputs.get("name_only"):
                model_name = outputs["name_only"].splitlines()[0].strip()

        # AMD ROCm
        elif accel_id == "amd":
            m = re.search(r"(\d+)\s*MB", outputs.get("mem", ""))
            if m:
                single_mib = int(m.group(1))
            cnt_raw = outputs.get("count", "").strip()
            if cnt_raw.isdigit() and int(cnt_raw) > 0:
                card_count = int(cnt_raw)
            name = outputs.get("name", "")
            m = re.search(r"(MI\d+\w*)", name, re.IGNORECASE)
            if m:
                model_name = m.group(1).upper()

        # 海光
        elif accel_id == "hygon":
            m = re.search(r"(\d+)\s*MB", outputs.get("mem", ""))
            if m:
                single_mib = int(m.group(1))
            cnt_raw = outputs.get("count", "").strip()
            if cnt_raw.isdigit() and int(cnt_raw) > 0:
                card_count = int(cnt_raw)
            else:
                card_count = len([l for l in lspci_out.splitlines() if l.strip()])

            name = outputs.get("name", "")
            hw = outputs.get("hw", "")
            hygon_evidence = "\n".join([name, hw, lspci_out])
            m = re.search(r"(BW3000|Z100\w*|K100(?:-AI)?\w*)", hygon_evidence, re.IGNORECASE)
            if m:
                model_name = m.group(1).upper()
            elif re.search(r"Card Series:\s*BW", name, re.IGNORECASE) and (
                re.search(r"\bDID\s+6320\b", hw, re.IGNORECASE)
                or "[1D94:6320]" in hygon_evidence.upper()
            ):
                model_name = "BW3000"

        # 昆仑芯（xpu-smi 格式）
        elif accel_id == "kunlun":
            q = outputs.get("query", "")
            prod = re.search(r"Product Name\s*:\s*(.+)", q)
            if prod:
                model_name = prod.group(1).strip()
            if not card_count:
                # 用 lspci 兜底
                card_count = len([l for l in lspci_out.splitlines() if l.strip()])

        # 昇腾
        elif accel_id == "ascend":
            overview = outputs.get("overview", "")
            mem_out = outputs.get("mem", "")
            board_out = outputs.get("board", "")
            joined_ascend = overview + "\n" + mem_out + "\n" + board_out

            # 卡数：优先用 overview 的型号关键字数（每张卡一行 "| 2  910B4 ..."）。
            # 不能用 joined_ascend 里的 "NPU ID :" 直接计数 —— v23+ 的 npu-smi info -l
            # 配合 -t memory / -t board 循环采集会让每张卡的 NPU ID 字段出现多次，
            # 导致 N 卡机器被算成 2N / 3N。
            cnt = len(re.findall(
                r"\b\d+\s+(?:910\w*|310\w*|Ascend\s*9\d{2}\w*)\b",
                overview, re.IGNORECASE
            ))
            if not cnt:
                # 兜底：从 NPU ID 字段提取集合去重（不论 mem/board 重复几次都只算一张）
                ids = set(re.findall(r"NPU\s*ID\s*:\s*(\d+)",
                                     joined_ascend, re.IGNORECASE))
                cnt = len(ids)
            if cnt:
                card_count = cnt

            # 显存：优先 `HBM Capacity(MB) : N`（最稳）
            m = re.search(r"HBM\s+Capacity\s*\(MB\)\s*:\s*(\d+)",
                          joined_ascend, re.IGNORECASE)
            if m and int(m.group(1)) > 100:
                single_mib = int(m.group(1))
            else:
                # 回落到 "X / Y MB" 模式（部分老版本 npu-smi 输出）
                mm = re.findall(r"(\d+)\s*/\s*(\d+)\s*MB", joined_ascend)
                if mm:
                    # 取最大的 Y 作为 HBM 容量（避免拿到 DDR 或 hugepage）
                    single_mib = max(int(y) for _, y in mm)

            # 型号关键字
            for kw in ["910C", "910B4", "910B3", "910B", "910A", "910", "310P", "310"]:
                if kw in joined_ascend:
                    model_name = "Ascend " + kw
                    break

        # Habana
        elif accel_id == "habana":
            ov = outputs.get("overview", "")
            cnt = len(re.findall(r"HL-\d+", ov))
            if cnt:
                card_count = cnt
            for kw in ["HL-325", "HL-225", "Gaudi3", "Gaudi2"]:
                if kw in ov:
                    model_name = kw
                    break

        # 兜底卡数
        if not card_count and lspci_out:
            card_count = len([l for l in lspci_out.splitlines() if l.strip()])
        if not card_count:
            card_count = 1

        return card_count, single_mib, model_name

    @staticmethod
    def _norm_card_name(s: str) -> str:
        """卡型号比较用的归一化：大小写、空格/下划线/连字符差异统一抹平。"""
        return re.sub(r"[\s_\-]+", "", (s or "")).upper()

    def _match_model_spec(self, accel: Dict, model_name: str,
                          outputs: Dict[str, str], lspci_out: str = "") -> Dict:
        """根据 model_name 在 profile.model_specs 中找匹配。

        归一化后做 substring 匹配，使 "RTX 4070 Ti SUPER" 和 "RTX-4070-TI-SUPER"
        被视为同一型号。spec 列表按"长字符串在前"维护，避免短型号截胡（如
        "RTX 4090 D" 必须排在 "RTX 4090" 之前）。
        """
        norm_model = self._norm_card_name(model_name)
        for spec in accel.get("model_specs", []):
            if self._norm_card_name(spec["match"]) in norm_model:
                return spec
        # 二次匹配：在所有 SMI 输出和 lspci 证据链里找（型号字段未抓到时兜底）
        norm_joined = self._norm_card_name(lspci_out + "\n" + "\n".join(outputs.values()))
        for spec in accel.get("model_specs", []):
            if self._norm_card_name(spec["match"]) in norm_joined:
                return spec
            for dev_id in spec.get("device_ids", []):
                if self._norm_card_name(dev_id) in norm_joined:
                    return spec
            for marker in spec.get("smi_markers", []):
                if self._norm_card_name(marker) in norm_joined:
                    return spec
            for sub_id in spec.get("subsystem_ids", []):
                norm_sub = self._norm_card_name(sub_id)
                if norm_sub and len(norm_sub) >= 4 and norm_sub in norm_joined:
                    return spec
        return accel.get("default_spec", {})


# ─────────────────────────────────────────────
# 软件采集
# ─────────────────────────────────────────────

class SoftwareCollector:
    def __init__(self, profile: Dict, registry: CommandRegistry):
        self.profile = profile
        self.reg = registry

    @staticmethod
    def _parse_visible_devices(raw: str) -> Optional[int]:
        raw = (raw or "").strip()
        if not raw:
            return None
        upper = raw.upper()
        if upper in ("ALL",):
            return None
        if upper in ("NONE", "VOID", "NO_DEVICES"):
            return 0
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return len(parts) if parts else None

    def _collect_runtime_visibility(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {}
        counts: List[int] = []
        for env_key in [
            "HIP_VISIBLE_DEVICES",
            "ROCR_VISIBLE_DEVICES",
            "CUDA_VISIBLE_DEVICES",
            "NVIDIA_VISIBLE_DEVICES",
        ]:
            raw = os.environ.get(env_key, "").strip()
            if not raw:
                continue
            info[env_key] = raw
            parsed = self._parse_visible_devices(raw)
            if parsed is not None:
                counts.append(parsed)
        if counts:
            info["容器可见卡数(按环境变量推断)"] = min(counts)
        else:
            info["容器可见卡数(按环境变量推断)"] = "未显式限制"
        return info

    def collect_os(self) -> Dict:
        info: Dict[str, Any] = {}
        _, uname, _ = run("uname -a 2>/dev/null")
        _, hostname, _ = run("hostname -f 2>/dev/null || hostname")
        _, os_rel, _ = run("cat /etc/os-release 2>/dev/null")

        os_dict: Dict[str, str] = {}
        for line in os_rel.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                os_dict[k.strip()] = v.strip().strip('"')

        info["主机名"]   = hostname
        info["内核版本"] = uname
        info["发行版"]   = os_dict.get("PRETTY_NAME", os_dict.get("NAME", "未知"))
        info["版本号"]   = os_dict.get("VERSION_ID", "未知")
        info["OS_ID"]    = os_dict.get("ID", "未知")

        _, uptime, _ = run("uptime -p 2>/dev/null || uptime")
        info["运行时长"] = uptime

        _, timectl, _ = run("timedatectl 2>/dev/null | head -12")
        info["时间同步"] = timectl or "无法获取"

        _, selinux, _ = run("getenforce 2>/dev/null")
        if selinux:
            info["SELinux"] = selinux

        _, apparmor, _ = run("apparmor_status 2>/dev/null | head -3")
        if apparmor:
            info["AppArmor"] = apparmor

        _, sysctl_net, _ = run(
            "sysctl 2>/dev/null | grep -E "
            "'net.core.rmem_max|net.core.wmem_max|net.ipv4.tcp_rmem|"
            "vm.nr_hugepages|vm.swappiness' | head -10"
        )
        if sysctl_net:
            info["关键内核参数"] = sysctl_net
        return info

    def collect_drivers(self, accel_info: Dict) -> Dict:
        """根据已检测到的加速卡，采集对应驱动版本。"""
        info: Dict[str, Any] = {}

        # NVIDIA
        if self.reg.has("nvidia-smi"):
            _, nv_drv, _ = run(
                "nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1"
            )
            if nv_drv:
                info["NVIDIA驱动"] = nv_drv
        if self.reg.has("nvcc"):
            _, cuda, _ = run("nvcc --version 2>/dev/null | grep release")
            if cuda:
                info["CUDA Toolkit"] = cuda

        # ROCm
        if self.reg.has("rocm-smi") or self.reg.has("rocminfo"):
            _, rocm_ver, _ = run(
                "cat /opt/rocm/.info/version 2>/dev/null || "
                "rocm-smi --version 2>/dev/null | head -3"
            )
            if rocm_ver:
                info["ROCm版本"] = rocm_ver

        # 海光 DTK
        _, dtk_ver, _ = run(
            "cat /opt/dtk/.info/version 2>/dev/null || "
            "hy-smi --version 2>/dev/null | head -3"
        )
        if dtk_ver:
            info["海光 DTK"] = dtk_ver

        # 昆仑 XPU
        if self.reg.has("xpu-smi"):
            _, xpu_drv, _ = run(
                "xpu-smi -q 2>/dev/null | grep -E 'Driver Version|XPU-RT Version' | head -3"
            )
            if xpu_drv:
                info["昆仑 XPU 驱动"] = xpu_drv

        # 昇腾 CANN
        for cann_path in [
            "/usr/local/Ascend/ascend-toolkit/latest",
            "/usr/local/Ascend/nnae/latest",
        ]:
            for sub in ["arm64-linux", "x86_64-linux"]:
                _, c, _ = run(
                    f"cat {cann_path}/{sub}/ascend_toolkit_install.info 2>/dev/null | head -3"
                )
                if c:
                    info["CANN 工具链"] = c
                    break
            if info.get("CANN 工具链"):
                break

        # 昇腾 NPU 驱动 / 固件版本
        for drv_path in [
            "/usr/local/Ascend/driver/version.info",
            "/usr/local/Ascend/firmware/version.info",
        ]:
            _, dv, _ = run(f"cat {drv_path} 2>/dev/null | head -3")
            if dv:
                key = "Ascend NPU 驱动" if "driver" in drv_path else "Ascend NPU 固件"
                info[key] = dv

        # npu-smi 工具版本（含 v25.2.3 等）
        if self.reg.has("npu-smi"):
            _, nsv, _ = run("npu-smi -v 2>/dev/null | head -1")
            if nsv:
                info["npu-smi 版本"] = nsv

        # MindIE 服务包（昇腾推理服务）
        for mindie_path in [
            "/usr/local/Ascend/mindie/latest",
            "/usr/local/Ascend/atb-models/latest",
        ]:
            _, mv, _ = run(f"cat {mindie_path}/version.info 2>/dev/null | head -3")
            if mv:
                key = "MindIE" if "mindie" in mindie_path else "ATB Models"
                info[key] = mv

        # hccn_tool 集合通信网络检测（NPU 互联）
        if self.reg.has("hccn_tool"):
            _, hct, _ = run("hccn_tool -i 0 -ip -g 2>/dev/null | head -3")
            if hct:
                info["HCCN (NPU 网络)"] = hct

        # Habana
        if self.reg.has("hl-smi"):
            _, hl_drv, _ = run("hl-smi -q 2>/dev/null | grep -i 'Driver Version' | head -1")
            if hl_drv:
                info["Habana 驱动"] = hl_drv

        # 网卡驱动
        _, ifaces, _ = run(
            "ip link show 2>/dev/null | grep '^[0-9]' | awk '{print $2}' | sed 's/://'"
        )
        net_drv: Dict[str, str] = {}
        if ifaces:
            for iface in ifaces.split("\n"):
                iface = iface.strip()
                if iface and iface != "lo":
                    _, et, _ = run(
                        f"ethtool -i {iface} 2>/dev/null | "
                        f"grep -E 'driver:|version:|firmware-version:'"
                    )
                    if et:
                        d = {}
                        for ln in et.splitlines():
                            if ":" in ln:
                                k, _, v = ln.partition(":")
                                d[k.strip()] = v.strip()
                        drv_key = d.get("driver", "?")
                        if drv_key not in net_drv:
                            net_drv[drv_key] = (
                                f"v{d.get('version','?')} / fw {d.get('firmware-version','?')}"
                            )
        if net_drv:
            info["网卡驱动 (按 driver 聚合)"] = net_drv

        if self.reg.has("ofed_info"):
            _, ofed, _ = run("ofed_info -s 2>/dev/null")
            if ofed:
                info["MLNX_OFED"] = ofed
        _, ib_fw, _ = run(
            "for f in /sys/class/infiniband/*/fw_ver; do "
            "[ -f \"$f\" ] && cat \"$f\"; done 2>/dev/null | sort -u"
        )
        if ib_fw:
            info["InfiniBand 固件版本"] = ib_fw.replace("\n", ", ")
        return info

    def collect_rdma_cluster(self) -> Dict:
        info: Dict[str, Any] = {}
        rdma_basic = self.reg.list("rdma_basic")
        rdma_perf  = self.reg.list("rdma_perftest")
        rdma_diag  = self.reg.list("rdma_diag")
        if rdma_basic or rdma_perf or rdma_diag:
            info["RDMA 工具集"] = {
                "基础工具": ", ".join(rdma_basic) or "无",
                "性能测试": ", ".join(rdma_perf) or "无",
                "诊断工具": ", ".join(rdma_diag) or "无",
            }

        mlx = self.reg.list("mellanox_mgmt")
        if mlx:
            info["Mellanox 管理工具"] = ", ".join(mlx)
            if self.reg.has("mlxconfig"):
                _, mst, _ = run("mst start 2>/dev/null; mst status 2>/dev/null | head -10")
                if "MST module" in mst or "/dev/mst" in mst:
                    info["mlxconfig MST 状态"] = mst

        mpi_impls: List[str] = []
        if self.reg.has("mpichversion"):
            _, mv, _ = run("mpichversion 2>/dev/null | head -3")
            if mv:
                mpi_impls.append(f"MPICH: {mv.splitlines()[0]}")
        if self.reg.has("ompi_info"):
            _, ov, _ = run("ompi_info --version 2>/dev/null | head -2")
            if ov:
                mpi_impls.append(f"OpenMPI: {ov}")
        _, impi, _ = run("ls /opt/intel*/mpi* 2>/dev/null | head -3")
        if impi:
            mpi_impls.append(f"Intel MPI: {impi}")
        _, hpcx, _ = run("ls /opt/hpcx* 2>/dev/null | head -3")
        if hpcx:
            mpi_impls.append(f"HPC-X: {hpcx}")
        if mpi_impls:
            info["MPI 实现"] = mpi_impls

        # 集合通信库（兼容 ldconfig 缓存未刷新的情况）
        ccl: Dict[str, str] = {}
        ld_cache, _, _ = run("ldconfig -p 2>/dev/null", timeout=15)
        _, ld_cache_str, _ = run("ldconfig -p 2>/dev/null")
        # 也搜索常见目录
        _, fs_libs, _ = run(
            "find /usr/lib /usr/local/lib /opt -maxdepth 5 -name 'lib*ccl*.so*' 2>/dev/null | head -20"
        )
        combined = ld_cache_str + "\n" + fs_libs
        for label, pat in [
            ("NCCL", r"nccl"),
            ("XCCL/BKCL (昆仑)", r"libxccl|libbkcl"),
            ("HCCL (昇腾)", r"hccl"),
            ("RCCL (AMD)", r"rccl"),
            ("CNCL (寒武纪)", r"cncl"),
            ("oneCCL (Intel)", r"oneccl|libccl"),
            ("GPUDirect gdrcopy", r"gdrapi|gdrcopy"),
        ]:
            m = [l for l in combined.splitlines() if re.search(pat, l, re.IGNORECASE)]
            if m:
                ccl[label] = m[0].strip()
        _, ucx, _ = run("ucx_info -v 2>/dev/null | head -3")
        if ucx:
            ccl["UCX"] = ucx
        if ccl:
            info["集合通信库 / GPUDirect"] = ccl

        sched = self.reg.list("scheduler")
        if sched:
            sched_info: Dict[str, str] = {}
            if "sbatch" in sched:
                _, sv, _ = run("sbatch --version 2>/dev/null")
                sched_info["Slurm"] = sv or "已安装"
            if "qsub" in sched:
                _, qv, _ = run("qstat --version 2>/dev/null || pbsnodes --version 2>/dev/null")
                sched_info["PBS/Torque"] = qv or "已安装"
            if "bsub" in sched:
                sched_info["LSF"] = "已安装"
            if sched_info:
                info["集群调度器"] = sched_info
        return info

    def collect_containers(self) -> Dict:
        info: Dict[str, Any] = {}
        if self.reg.has("docker"):
            _, dv, _ = run("docker --version 2>/dev/null")
            _, ds, _ = run("docker info 2>/dev/null | head -25")
            info["Docker"] = {"版本": dv, "info摘要": ds or "[需要权限]"}
            _, dr, _ = run(
                "docker info 2>/dev/null | "
                "grep -E 'Server Version|Storage Driver|Cgroup Driver|Runtimes|Default Runtime'"
            )
            if dr:
                info["Docker"]["关键配置"] = dr

        if self.reg.has("containerd"):
            _, cv, _ = run("containerd --version 2>/dev/null")
            info["containerd"] = cv
        if self.reg.has("podman"):
            _, pv, _ = run("podman --version 2>/dev/null")
            info["Podman"] = pv
        if self.reg.has("nerdctl"):
            _, nv, _ = run("nerdctl --version 2>/dev/null")
            info["nerdctl"] = nv
        if self.reg.has("crictl"):
            _, cv, _ = run("crictl --version 2>/dev/null")
            info["crictl"] = cv

        gpu_ctk: Dict[str, str] = {}
        if self.reg.has("nvidia-ctk"):
            _, v, _ = run("nvidia-ctk --version 2>/dev/null")
            gpu_ctk["NVIDIA Container Toolkit"] = v
        if self.reg.has("xpu-ctk"):
            _, v, _ = run("xpu-ctk --version 2>/dev/null | head -2")
            gpu_ctk["XPU Container Toolkit (昆仑)"] = v
        if self.reg.has("habana-container-cli"):
            gpu_ctk["Habana Container CLI"] = "已安装"
        if self.reg.has("ix-container-runtime"):
            gpu_ctk["Iluvatar Container Runtime"] = "已安装"
        if gpu_ctk:
            info["GPU 容器工具"] = gpu_ctk

        k8s: Dict[str, str] = {}
        for tool in ["kubectl", "kubeadm", "kubelet", "k3s", "microk8s", "k0s", "helm"]:
            if self.reg.has(tool):
                _, v, _ = run(f"{tool} version --short 2>/dev/null || {tool} --version 2>/dev/null | head -2")
                k8s[tool] = v or "已安装"
        if k8s:
            info["Kubernetes 工具链"] = k8s
        if self.reg.has("kubectl"):
            _, kn, _ = run("kubectl get nodes 2>/dev/null | head -10", timeout=15)
            if kn and "NAME" in kn:
                info["K8s 节点状态"] = kn

        # 环境检测：宿主机 / Docker / K8S
        env_type = "宿主机"
        if os.path.exists("/.dockerenv"):
            env_type = "docker容器"
        elif os.path.exists("/run/.containerenv"):
            # 可能是Podman、Podman-compose、Kubernetes等
            if os.path.exists("/var/run/secrets/kubernetes.io") or os.environ.get("KUBERNETES_SERVICE_HOST"):
                env_type = "K8S容器"
            else:
                env_type = "docker容器"
        elif os.path.exists("/var/run/secrets/kubernetes.io") or os.environ.get("KUBERNETES_SERVICE_HOST"):
            env_type = "K8S容器"
        info["当前脚本运行环境"] = env_type
        return info

    def collect_ml_env(self) -> Dict:
        info: Dict[str, Any] = {}
        _, py, _ = run("python3 --version 2>/dev/null || python --version 2>/dev/null")
        info["Python"] = py or "未安装"

        _, conda, _ = run("conda --version 2>/dev/null")
        if conda:
            info["Conda"] = conda

        runtime_visibility = self._collect_runtime_visibility()

        _, torch_raw, _ = run(
            "python3 -c 'import json, torch; "
            "print(json.dumps({"
            "\"version\": torch.__version__, "
            "\"cuda_available\": torch.cuda.is_available(), "
            "\"device_count\": torch.cuda.device_count(), "
            "\"cuda_version\": getattr(torch.version, \"cuda\", None), "
            "\"hip_version\": getattr(torch.version, \"hip\", None)"
            "}, ensure_ascii=False))' 2>/dev/null"
        )
        torch_info: Dict[str, Any] = {}
        if torch_raw:
            try:
                torch_info = json.loads(torch_raw)
            except json.JSONDecodeError:
                torch_info = {}

        if torch_info:
            backend = "HIP/DTK" if torch_info.get("hip_version") else (
                "CUDA" if torch_info.get("cuda_version") else "unknown"
            )
            info["PyTorch"] = (
                f"{torch_info.get('version', '未知版本')} · "
                f"Backend: {backend} · "
                f"CUDA API: {torch_info.get('cuda_available')} · "
                f"Devices: {torch_info.get('device_count', 0)}"
            )
            runtime_visibility["PyTorch可见卡数"] = torch_info.get("device_count", 0)
            runtime_visibility["PyTorch后端"] = backend
            if torch_info.get("hip_version"):
                runtime_visibility["HIP Runtime"] = torch_info["hip_version"]
            elif torch_info.get("cuda_version"):
                runtime_visibility["CUDA Runtime"] = torch_info["cuda_version"]
        else:
            info["PyTorch"] = "未安装"

        info["运行时可见性"] = runtime_visibility

        for pkg, label in [
            ("vllm", "vLLM"),
            ("lmdeploy", "LMDeploy"),
            ("sglang", "SGLang"),
            ("transformers", "Transformers"),
            ("paddle", "PaddlePaddle"),
            ("paddlenlp", "PaddleNLP"),
            ("mindspore", "MindSpore"),
            ("fastdeploy", "FastDeploy"),
            ("habana_frameworks", "Habana Frameworks"),
            # NPU 后端（昇腾）
            ("torch_npu", "torch_npu"),
            ("vllm_ascend", "vLLM-Ascend"),
            ("mindie", "MindIE"),
        ]:
            _, v, _ = run(f"python3 -c 'import {pkg}; print({pkg}.__version__)' 2>/dev/null")
            if v:
                info[label] = v
            elif pkg in ("vllm", "lmdeploy", "sglang", "transformers"):
                info[label] = "未安装"

        for cli in ["llama-server", "llama-cli"]:
            if self.reg.has(cli):
                _, v, _ = run(f"{cli} --version 2>/dev/null | head -2")
                info["llama.cpp"] = v or "已安装"
                break

        _, pkgs, _ = run(
            "pip3 list 2>/dev/null | "
            "grep -iE 'torch|vllm|sglang|lmdeploy|paddle|mindspore|transformers|xdnn|xccl|musa|maca'"
        )
        if pkgs:
            info["相关 pip 包"] = pkgs
        return info


# ─────────────────────────────────────────────
# 报告生成
# ─────────────────────────────────────────────

class ReportGenerator:
    def __init__(self, hw: Dict, sw: Dict, hostname: str, profile: Dict):
        self.hw = hw
        self.sw = sw
        self.profile = profile
        # 主机名兜底：抓不到时按运行环境给一个概括性描述
        h = (hostname or "").strip().split(".")[0]
        if not h:
            env = sw.get("容器与K8s", {}).get("当前脚本运行环境", "宿主机")
            h = {"docker容器": "Docker 容器",
                 "K8S容器":     "K8S 容器",
                 "宿主机":       "物理服务器"}.get(env, "物理服务器")
        self.hostname = h
        self.ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.tool_version = profile.get("tool_version", "v1.2.0")
        accel = hw.get("算力卡", {})
        self.accel_type     = accel.get("_主要算力类型", "未检测到")
        self.accel_count    = accel.get("_总卡数", 0)
        self.total_vram_mib = accel.get("_总显存MiB", 0)
        self.detected_cards = accel.get("_detected_cards", [])
        self.accel_mem_gb   = (
            self.total_vram_mib / max(self.accel_count, 1) / 1024
        )
        self.total_vram_gb = self.total_vram_mib / 1024
        self.primary_card  = self.detected_cards[0] if self.detected_cards else None
        # 新硬件贡献：自动检测当前环境是否含数据库未收录的硬件
        self.unknown_items = self._detect_unknown_hardware()
        # 贡献文件路径由 main() 写完后回填
        self.contribution_path: Optional[str] = None

    def _detect_unknown_hardware(self) -> List[Dict]:
        """识别 CPU/GPU/加速卡中数据库未收录的项。返回结构化清单。"""
        items: List[Dict] = []
        accel = self.hw.get("算力卡", {})

        # 1. 没有任何加速卡，但 lspci 看到 display/accelerator 类设备
        if accel.get("_无算力卡") and accel.get("其他PCIe加速设备"):
            items.append({
                "type": "gpu",
                "summary": "lspci 检测到 PCIe display/accelerator 设备，但所有厂商 SMI/lspci_pattern 均未命中",
                "details": {"pcie_devices": accel["其他PCIe加速设备"]},
            })

        # 2. 卡被识别到厂商但 model_name 没命中 model_specs（落到 default_spec）
        for card in self.detected_cards:
            model_name = card.get("_model_name", "")
            spec = card.get("_spec", {})
            # default_spec 是没有 "match" 字段的 dict；model_specs 条目必有 "match"
            falled_to_default = "match" not in spec
            if falled_to_default or not model_name:
                items.append({
                    "type": "gpu",
                    "summary": (f"{card.get('_display_name','?')} 检测到 "
                                f"× {card.get('_card_count','?')} 卡，"
                                f"但卡型号未匹配数据库 model_specs"),
                    "details": {
                        "display_name":      card.get("_display_name", ""),
                        "vendor":            card.get("_vendor", ""),
                        "accel_id":          card.get("_accel_id", ""),
                        "model_name_raw":    model_name or "",
                        "card_count":        card.get("_card_count", 0),
                        "single_vram_mib":   card.get("_single_vram_mib", 0),
                        "pcie_devices":      card.get("PCIe设备列表", ""),
                        "smi_summary":       card.get("命令摘要", {}),
                    },
                })

        # 3. CPU 厂商未识别
        cpu = self.hw.get("CPU", {})
        cpu_vendor = (cpu.get("厂商", "") or "").strip()
        cpu_model = cpu.get("型号", "")
        if not cpu_vendor or cpu_vendor in ("未识别", "未知", "Unknown"):
            items.append({
                "type": "cpu",
                "summary": f"CPU 厂商无法识别（型号: {cpu_model or '未知'}）",
                "details": {
                    "vendor_raw":  cpu_vendor or "",
                    "model":       cpu_model,
                    "arch":        cpu.get("架构", ""),
                    "phys_cpus":   cpu.get("物理CPU数", ""),
                    "cores_per":   cpu.get("每CPU核数", ""),
                    "threads":     cpu.get("总线程数", ""),
                    "vector_isa":  cpu.get("向量指令集", ""),
                },
            })

        return items

    def analyze(self) -> Dict:
        issues: List[str] = []
        warnings: List[str] = []
        goods: List[str] = []

        total_mem_str = self.hw.get("内存", {}).get("总容量", "0")
        m = re.search(r"([\d.]+)\s*([GTM])", total_mem_str, re.I)
        total_gb = 0.0
        if m:
            v, u = float(m.group(1)), m.group(2).upper()
            total_gb = v if u == "G" else (v * 1024 if u == "T" else v / 1024)
        if total_gb >= 1024:
            goods.append(f"系统内存充裕: {total_mem_str}，支持超大模型 KV Cache")
        elif total_gb >= 128:
            goods.append(f"系统内存: {total_mem_str}")
        elif 0 < total_gb < 64:
            warnings.append(f"系统内存偏小 ({total_mem_str})")

        if self.accel_count == 0 or "未检测到" in self.accel_type:
            issues.append("未检测到算力加速卡，仅支持 CPU 推理")
        else:
            for c in self.detected_cards:
                goods.append(
                    f"算力卡: {c['_display_name']} {c.get('卡型号','?')} × {c['_card_count']} 卡，"
                    f"单卡 {c['_single_vram_mib']/1024:.0f} GB"
                )

        rdma_info = self.sw.get("RDMA与集群", {}).get("RDMA 工具集", {})
        if rdma_info:
            parts = []
            if rdma_info.get("基础工具", "无") != "无":
                parts.append("基础")
            if rdma_info.get("性能测试", "无") != "无":
                parts.append("perftest")
            if rdma_info.get("诊断工具", "无") != "无":
                parts.append("诊断")
            if parts:
                goods.append(f"RDMA 工具集已安装（{' / '.join(parts)}）")
        rdma_link = self.hw.get("网络", {}).get("RDMA链路状态(ibstatus)", "")
        if "ACTIVE" in rdma_link:
            active_cnt = rdma_link.count("ACTIVE")
            rate_m = re.search(r"(\d+)\s*Gb/sec", rdma_link)
            rate = rate_m.group(1) if rate_m else "?"
            goods.append(f"RDMA 链路 {active_cnt} 路 Active，速率 {rate} Gb/s")

        if self.sw.get("RDMA与集群", {}).get("MPI 实现"):
            goods.append("MPI 已安装，支持多机分布式推理/训练")

        ctr = self.sw.get("容器与K8s", {})
        runtime_env = ctr.get("当前脚本运行环境", "宿主机")
        in_container = runtime_env != "宿主机"
        if ctr.get("Docker"):
            goods.append("Docker 已安装")
        gpu_ctk = ctr.get("GPU 容器工具", {})
        if gpu_ctk:
            tools = ", ".join(gpu_ctk.keys())
            goods.append(f"GPU 容器工具链: {tools}")
        elif not in_container:
            # 容器内本就不需要装 nvidia-container-toolkit / ascend-docker-runtime，
            # 它们部署在宿主机；只在宿主机环境且没装时才提示
            warnings.append("未检测到 GPU 容器工具，容器内调用加速卡会受限")
        if ctr.get("Kubernetes 工具链"):
            goods.append("Kubernetes 工具链已安装")

        ml = self.sw.get("ML推理框架", {})
        if ml.get("vLLM", "未安装") != "未安装":
            goods.append(f"vLLM 已安装 v{ml['vLLM']}")
        else:
            warnings.append("vLLM 未安装")

        runtime_vis = ml.get("运行时可见性", {})
        torch_visible = runtime_vis.get("PyTorch可见卡数")
        env_visible = runtime_vis.get("容器可见卡数(按环境变量推断)")
        if isinstance(torch_visible, int) and self.accel_count and torch_visible < self.accel_count:
            warnings.append(
                f"运行时仅暴露 {torch_visible} 张加速卡，低于物理检测到的 {self.accel_count} 张；"
                "容器或调度器侧可能做了设备可见性限制"
            )
        elif isinstance(env_visible, int) and self.accel_count and env_visible < self.accel_count:
            warnings.append(
                f"环境变量推断当前仅允许访问 {env_visible} 张加速卡，"
                f"低于物理检测到的 {self.accel_count} 张"
            )

        # 特定加速卡建议：只在该卡的"所有官方推理框架都没装"时才提示
        if self.primary_card:
            pid = self.primary_card.get("_accel_id", "")
            if pid == "kunlun":
                # 昆仑芯：PaddleNLP / FastDeploy / vLLM-XPU 任一个就够
                kunlun_alts = [ml.get("PaddleNLP"), ml.get("FastDeploy"), ml.get("vLLM")]
                if not any(v and v != "未安装" for v in kunlun_alts):
                    warnings.append("昆仑芯首选推理框架 PaddleNLP 未安装")
            if pid == "ascend":
                # 昇腾：MindSpore / MindIE / vLLM-Ascend / LMDeploy-Ascend 任一就够
                ascend_alts = [ml.get("MindSpore"), ml.get("MindIE"),
                               ml.get("vLLM-Ascend"), ml.get("torch_npu"),
                               ml.get("vLLM"), ml.get("LMDeploy")]
                if not any(v and v != "未安装" for v in ascend_alts):
                    warnings.append("昇腾未检测到官方推理框架（MindIE/MindSpore/vLLM-Ascend/LMDeploy-Ascend 任一）")
            if pid == "habana" and not ml.get("Habana Frameworks"):
                warnings.append("Gaudi 首选框架 habana_frameworks 未安装")
        return {"问题": issues, "警告": warnings, "正常": goods}

    def _has_rdma_capability(self) -> bool:
        """检查RDMA链路是否可用（ACTIVE状态）。"""
        rdma_link = self.hw.get("网络", {}).get("RDMA链路状态(ibstatus)", "")
        if not rdma_link:
            return False
        active_cnt = rdma_link.count("ACTIVE")
        return active_cnt >= 2  # 至少2路ACTIVE

    @staticmethod
    def _parse_numeric_scale(text: str, default: float = 0.0) -> float:
        m = re.search(r"([\d.]+)", str(text or ""))
        return float(m.group(1)) if m else default

    @staticmethod
    def _parse_context_tokens(text: str) -> int:
        raw = (text or "").strip().upper().replace(" ", "")
        m = re.search(r"([\d.]+)([KM]?)", raw)
        if not m:
            return 32768
        value = float(m.group(1))
        unit = m.group(2)
        if unit == "M":
            return int(value * 1024 * 1024)
        if unit == "K":
            return int(value * 1024)
        return int(value)

    @staticmethod
    def _architecture_of(model: Dict) -> str:
        params = str(model.get("params", "")).lower()
        return "moe" if "moe" in params else "dense"

    def _candidate_tp_sizes(self) -> List[int]:
        cnt = max(self.accel_count, 0)
        if cnt <= 0:
            return [1]
        vals = [1]
        for v in [2, 4, 8]:
            if v <= cnt:
                vals.append(v)
        if cnt not in vals:
            vals.append(cnt)
        return sorted(set(vals))

    def _precision_weight_gb(self, model: Dict, precision: str) -> float:
        precision = precision.upper()
        if precision.startswith("BF16") or precision.startswith("FP16"):
            return float(model.get("fp16_gb", 0))
        if precision.startswith("INT8"):
            return float(model.get("int8_gb", round(float(model.get("fp16_gb", 0)) / 2)))
        return float(model.get("int4_gb", 0))

    def _runtime_overhead_gb(self, model: Dict, precision: str,
                             weight_per_card_gb: float, tp: int) -> float:
        accel_id = self.primary_card.get("_accel_id", "") if self.primary_card else ""
        arch = self._architecture_of(model)
        ratio = 0.18 if arch == "dense" else 0.15
        if precision.upper().startswith("INT"):
            ratio *= 0.90
        vendor_multiplier = 1.0
        reserved_gb = 2.0
        if accel_id == "hygon":
            vendor_multiplier = 1.10
            reserved_gb = 3.0
        elif accel_id in ("kunlun", "ascend", "cambricon", "biren", "metax", "moorethreads"):
            vendor_multiplier = 1.05
            reserved_gb = 2.5
        # TP 越高，通信与运行时缓冲开销越大
        tp_penalty = 1.0 + max(0, tp - 1) * 0.03
        return weight_per_card_gb * ratio * vendor_multiplier * tp_penalty + reserved_gb

    def _kv_per_sequence_gb(self, model: Dict, tp: int) -> float:
        total_b = self._parse_numeric_scale(model.get("params", ""), 0.0)
        active_b = self._parse_numeric_scale(model.get("active", ""), total_b)
        context_tokens = self._parse_context_tokens(model.get("context", "32K"))
        context_scale = max(0.25, context_tokens / 131072.0)
        if self._architecture_of(model) == "moe":
            model_scale = max(total_b / 16.0, active_b / 4.0)
        else:
            model_scale = total_b / 16.0
        kv_gb = context_scale * model_scale * 1.1 / max(tp, 1)
        return max(0.5, kv_gb)

    def _plan_for_model(self, model: Dict, precision: str, tp: int) -> Dict[str, Any]:
        if not self.primary_card or self.accel_count <= 0:
            return {"ok": False, "reason": "无可用加速卡"}
        if tp > self.accel_count:
            return {"ok": False, "reason": f"当前仅检测到 {self.accel_count} 张卡"}

        single_mem_gb = self.accel_mem_gb
        usable_per_card_gb = single_mem_gb * 0.90
        total_weight_gb = self._precision_weight_gb(model, precision)
        weight_per_card_gb = total_weight_gb / max(tp, 1)
        runtime_overhead_gb = self._runtime_overhead_gb(model, precision, weight_per_card_gb, tp)
        kv_budget_gb = usable_per_card_gb - weight_per_card_gb - runtime_overhead_gb
        kv_per_seq_gb = self._kv_per_sequence_gb(model, tp)
        max_concurrency = int(kv_budget_gb / kv_per_seq_gb) if kv_budget_gb > 0 else 0

        if kv_budget_gb <= 0:
            return {
                "ok": False,
                "tp": tp,
                "precision": precision,
                "weight_per_card_gb": weight_per_card_gb,
                "runtime_overhead_gb": runtime_overhead_gb,
                "kv_budget_gb": kv_budget_gb,
                "kv_per_seq_gb": kv_per_seq_gb,
                "max_concurrency": 0,
                "reason": (
                    f"单卡可用 {usable_per_card_gb:.1f}GB，扣除权重 {weight_per_card_gb:.1f}GB 和"
                    f" 运行时开销 {runtime_overhead_gb:.1f}GB 后，KV 预算为 {kv_budget_gb:.1f}GB"
                ),
            }
        if max_concurrency < 1:
            return {
                "ok": False,
                "tp": tp,
                "precision": precision,
                "weight_per_card_gb": weight_per_card_gb,
                "runtime_overhead_gb": runtime_overhead_gb,
                "kv_budget_gb": kv_budget_gb,
                "kv_per_seq_gb": kv_per_seq_gb,
                "max_concurrency": 0,
                "reason": (
                    f"权重可装载，但目标上下文下单序列 KV 约需 {kv_per_seq_gb:.1f}GB/卡，"
                    f"当前仅剩 {kv_budget_gb:.1f}GB/卡"
                ),
            }
        return {
            "ok": True,
            "tp": tp,
            "precision": precision,
            "weight_per_card_gb": weight_per_card_gb,
            "runtime_overhead_gb": runtime_overhead_gb,
            "kv_budget_gb": kv_budget_gb,
            "kv_per_seq_gb": kv_per_seq_gb,
            "max_concurrency": max_concurrency,
            "usable_per_card_gb": usable_per_card_gb,
        }

    def _best_plan_for_model(self, model: Dict) -> Optional[Dict[str, Any]]:
        for precision in ["BF16", "INT8", "INT4"]:
            if self._precision_weight_gb(model, precision) <= 0:
                continue
            for tp in self._candidate_tp_sizes():
                plan = self._plan_for_model(model, precision, tp)
                if plan.get("ok"):
                    return plan
        return None

    def _benchmark_commands(self) -> List[Dict[str, str]]:
        cmds: List[Dict[str, str]] = []
        for model in self.evaluate_models():
            plan = model.get("_best_plan")
            if not plan:
                continue
            max_len = self._parse_context_tokens(model.get("context", "32K"))
            cmds.append({
                "模型": model["name"],
                "建议": f"{plan['precision']} / TP={plan['tp']}",
                "命令": (
                    "vllm serve <model_path> "
                    f"--tensor-parallel-size {plan['tp']} "
                    "--gpu-memory-utilization 0.5 "
                    "--max-num-seqs 1 "
                    f"--max-model-len {max_len} "
                    "--enforce-eager"
                ),
            })
            if len(cmds) >= 2:
                break
        return cmds

    def _performance_confidence(self) -> str:
        accel_id = self.primary_card.get("_accel_id", "") if self.primary_card else ""
        if accel_id == "nvidia":
            return "中"
        if accel_id in ("hygon", "kunlun", "ascend", "cambricon", "biren", "metax", "moorethreads"):
            return "低"
        return "中低"

    def _estimate_perf_bounds(self, model: Dict, plan: Dict[str, Any],
                              actual_cards: int) -> Dict[str, str]:
        spec = self.primary_card.get("_spec", {}) if self.primary_card else {}
        bw_tbps = float(spec.get("hbm_bw_tbps", 0) or 0)
        bf16_tflops = float(spec.get("bf16_tflops", 0) or 0)
        accel_id = self.primary_card.get("_accel_id", "") if self.primary_card else ""

        decode_eff = (0.70, 0.85) if accel_id == "nvidia" else (0.35, 0.55)
        prefill_eff = (0.35, 0.50) if accel_id == "nvidia" else (0.18, 0.30)

        total_weight_gb = self._precision_weight_gb(model, plan["precision"])
        weight_per_card_gb = max(plan.get("weight_per_card_gb", 0.0), total_weight_gb / max(actual_cards, 1), 1.0)
        active_b = self._parse_numeric_scale(model.get("active", ""), 10.0)
        total_bw_gb = bw_tbps * 1024 * max(actual_cards, 1)
        total_tflops = bf16_tflops * max(actual_cards, 1)

        decode_lo = max(1, int((total_bw_gb * decode_eff[0]) / weight_per_card_gb))
        decode_hi = max(decode_lo + 1, int((total_bw_gb * decode_eff[1]) / weight_per_card_gb))

        # 用激活参数量做粗略 prefill 上限估算，强调这是硬件边界而非框架实测
        compute_divisor = max(active_b * 10.0, 20.0)
        prefill_lo = max(1, int((total_tflops * prefill_eff[0]) / compute_divisor))
        prefill_hi = max(prefill_lo + 1, int((total_tflops * prefill_eff[1]) / compute_divisor))

        penalty = "预计可达边界的 70%-90%" if accel_id == "nvidia" else "实际通常仅能达到该边界的 40%-70%"
        return {
            "Decode 上限": f"{decode_lo}-{decode_hi} tok/s",
            "Prefill 上限": f"{prefill_lo}-{prefill_hi} tok/s",
            "并发上限": str(plan.get("max_concurrency", "—")),
            "置信度": self._performance_confidence(),
            "说明": (
                f"按 {plan['precision']} / TP={plan['tp']} 估算；"
                f"国产卡场景下 {penalty}"
            ),
        }

    def recommend(self) -> Dict:
        cnt = self.accel_count
        has_rdma = self._has_rdma_capability()

        if cnt == 0:
            mode, tp = "CPU 推理（性能有限）", "1"
            frameworks = ["llama.cpp (CPU)", "Transformers + bitsandbytes"]
        else:
            candidate_models = self.evaluate_models()
            viable = [m for m in candidate_models if m.get("_best_plan")]
            if viable:
                target = sorted(
                    viable,
                    key=lambda m: (
                        m["_best_plan"]["tp"],
                        -self._precision_weight_gb(m, m["_best_plan"]["precision"]),
                    )
                )[0]
                best = target["_best_plan"]
                if best["tp"] == 1:
                    mode = "单卡优先，多副本扩展吞吐"
                elif best["tp"] <= 8:
                    mode = f"按模型动态并行（优先最小可行 TP={best['tp']}）"
                else:
                    mode = f"多机多卡（建议从 TP={best['tp']} 起步）"
                tp = f"{best['tp']}（参考模型：{target['name']} / {best['precision']}）"
            else:
                mode = "单机多卡（当前更适合作为量化试验或多机扩展节点）"
                tp = "1/2/4/8（按模型最小可行值）"
                if has_rdma and cnt >= 8:
                    mode = "多机多卡（本机单独难承载主流大模型）"

        if self.primary_card:
            frameworks = self.primary_card.get("_frameworks", ["llama.cpp (CPU)"])
        elif cnt == 0:
            pass
        else:
            frameworks = ["llama.cpp"]

        return {
            "部署方式":   mode,
            "张量并行度": tp,
            "单卡显存":   f"{self.accel_mem_gb:.0f} GB",
            "总显存":     f"{self.total_vram_gb:.0f} GB",
            "推荐框架":   frameworks,
        }

    def evaluate_models(self) -> List[Dict]:
        results = []
        for m in self.profile.get("models_2026", []):
            r = dict(m)
            fp16_gb = float(m["fp16_gb"])
            int4_gb = float(m["int4_gb"])
            int8_gb = float(m.get("int8_gb", round(fp16_gb / 2)))
            r["int8_gb"] = int8_gb

            best_plan = self._best_plan_for_model(m)
            r["_best_plan"] = best_plan

            if self.total_vram_gb == 0:
                r["状态"] = "❌ 无算力卡"
                r["建议精度"] = "—"
                r["推荐并行"] = "—"
                r["理论最大并发"] = "—"
            elif best_plan:
                r["建议精度"] = best_plan["precision"]
                r["推荐并行"] = f"TP={best_plan['tp']}"
                r["理论最大并发"] = str(best_plan["max_concurrency"])
                r["状态"] = (
                    f"✅ 单卡可用 {best_plan['usable_per_card_gb']:.1f}GB，"
                    f"扣除权重 {best_plan['weight_per_card_gb']:.1f}GB 和运行时开销 "
                    f"{best_plan['runtime_overhead_gb']:.1f}GB 后，"
                    f"剩余 KV 预算 {best_plan['kv_budget_gb']:.1f}GB/卡；"
                    f"按 {m['context']} 上下文粗估最大并发约 {best_plan['max_concurrency']}"
                )
            else:
                best_attempt = None
                for precision in ["BF16", "INT8", "INT4"]:
                    for tp in self._candidate_tp_sizes():
                        attempt = self._plan_for_model(m, precision, tp)
                        if best_attempt is None or attempt.get("kv_budget_gb", -10**9) > best_attempt.get("kv_budget_gb", -10**9):
                            best_attempt = attempt
                r["建议精度"] = "—"
                r["推荐并行"] = "需更多卡 / 更低上下文"
                r["理论最大并发"] = "0"
                if best_attempt:
                    r["状态"] = f"❌ {best_attempt['reason']}"
                else:
                    r["状态"] = "❌ 当前硬件无法形成可用部署方案"

            # 框架适配
            vendor_match = False
            if self.primary_card:
                vendor_kw = self.primary_card["_display_name"]
                for tag in m.get("accel_support", []):
                    if tag.lower() in vendor_kw.lower() or tag in vendor_kw:
                        vendor_match = True
                        break
            if vendor_match:
                r["框架适配"] = "✅ " + " / ".join(m["frameworks"][:2])
            else:
                r["框架适配"] = "⚠️ 当前卡型可能需手工适配"
            results.append(r)
        return results

    def perf_scenarios(self) -> List[Dict]:
        """性能边界估算：按场景输出 Decode / Prefill 的理论上限。"""
        scns: List[Dict] = []
        if not self.primary_card:
            return scns

        card = self.primary_card
        spec = card["_spec"]
        cnt = card["_card_count"]
        single_gb = card["_single_vram_mib"] / 1024
        bf16_tflops = spec.get("bf16_tflops", 0)
        card_name = card.get("卡型号", "未知型号")
        display = card["_display_name"]
        frameworks = card.get("_frameworks", [])
        interconnect = spec.get("interconnect", "未知")
        models = self.profile.get("models_2026", [])

        for tpl in self.profile.get("scenario_templates", []):
            min_cards = tpl.get("min_cards", 1)
            max_cards = tpl.get("max_cards", 9999)
            if cnt < min_cards:
                continue
            if cnt > max_cards:
                continue
            # 双机场景下，要求至少 8 卡（默认假定双机 = 当前节点×2 推算）
            if tpl.get("id") == "multi_node_16" and cnt < 8:
                continue
            # 各场景实际并行卡数
            tpl_id = tpl.get("id", "")
            if tpl_id == "single_card":
                actual_cards = 1
            elif tpl_id == "multi_node_16":
                actual_cards = cnt * 2  # 假设双节点
            else:
                actual_cards = min(cnt, max_cards) if max_cards != 9999 else cnt

            total_gb = single_gb * actual_cards
            total_tflops = bf16_tflops * actual_cards
            tflops_str = (
                f"{total_tflops/1000:.2f} PFLOPS" if total_tflops >= 1000
                else f"{total_tflops:.0f} TFLOPS"
            )

            preferred_model_name = tpl.get("preferred_model", "")
            applicable: List[Dict] = []
            preferred_found = False

            for model in models:
                model_plan = model.get("_best_plan") if "_best_plan" in model else None
                if not model_plan:
                    model_plan = self._best_plan_for_model(model)
                if not model_plan:
                    continue
                if model_plan["tp"] > actual_cards:
                    continue

                perf = self._estimate_perf_bounds(model, model_plan, max(model_plan["tp"], actual_cards))
                entry = {
                    "模型": model["name"],
                    "推荐精度": model_plan["precision"],
                    "参考并行": f"TP={model_plan['tp']}",
                    "Decode 上限": perf["Decode 上限"],
                    "Prefill 上限": perf["Prefill 上限"],
                    "理论最大并发": perf["并发上限"],
                    "置信度": perf["置信度"],
                    "说明": perf["说明"],
                    "_preferred": model["name"] == preferred_model_name,
                }
                applicable.append(entry)
                if entry["_preferred"]:
                    preferred_found = True

            if not applicable:
                continue

            # 优先排列 preferred_model，其次优先更小 TP
            if preferred_found:
                applicable.sort(key=lambda x: (not x["_preferred"], x["参考并行"]))
            else:
                applicable.sort(key=lambda x: x["参考并行"])

            for item in applicable:
                del item["_preferred"]

            applicable = applicable[:3]

            entry = {
                "场景": tpl["name"],
                "硬件": f"{actual_cards} × {display} {card_name} = "
                        f"{total_gb:.0f} GB HBM / {tflops_str} BF16",
                "适用模型": applicable,
                "推理框架": " / ".join(frameworks[:2]),
                "建议用途": tpl.get("use_case", ""),
                "口径": "理论边界估算（非实测值）",
            }
            if tpl.get("id") == "multi_node_16":
                entry["通信"] = f"节点内 {interconnect} + 节点间 RoCE/NDR 400Gb"
            if tpl.get("prerequisites"):
                entry["前置条件"] = tpl["prerequisites"]
            scns.append(entry)

        return scns

    def _active_param_tier(self, active_b: float) -> str:
        """将激活参数量映射到 NVIDIA 基准的档位 (tiny/small/medium/large)。"""
        tiers = self.profile.get("nvidia_reference_benchmarks", {}).get("_active_param_tiers", {})
        for tier_name, tier in tiers.items():
            lo, hi = tier.get("range_b", [0, 0])
            if lo <= active_b <= hi:
                return tier_name
        # 超出 large 的（>80B 激活），归到 large（已是上限）
        return "large" if active_b > 5 else "tiny"

    def _resolve_cards_key(self, match_str: str, cards: Dict) -> Optional[str]:
        """根据 spec.match（如 "A100" / "RTX 4070 Ti SUPER"）在 cards 表里找对应 key。
        归一化后做完全 / 前缀 / 双向 substring 匹配，对 "A100" → "A100-80GB-SXM"
        以及 "RTX 4070 Ti SUPER" → "RTX-4070-TI-SUPER" 都能命中。
        """
        if not match_str:
            return None
        norm_q = HardwareCollector._norm_card_name(match_str)
        norm_map = {HardwareCollector._norm_card_name(k): k for k in cards.keys()}
        if norm_q in norm_map:
            return norm_map[norm_q]
        # 双向 substring：spec.match 是 cards key 前缀（A100 → A10080GBSXM），或反之
        for nk, ok in norm_map.items():
            if nk.startswith(norm_q) or norm_q.startswith(nk) or norm_q in nk or nk in norm_q:
                return ok
        return None

    def _lookup_nvidia_perf(self, scenario_id: str, active_b: float,
                            actual_cards: int = 0) -> Optional[Dict[str, List[int]]]:
        """查询 primary_card 对标的 NVIDIA 卡在指定场景+档位的基准数据。
        返回 None 表示无法对标（缺少 nvidia_equivalent 或基准未配置）。
        """
        if not self.primary_card:
            return None
        spec = self.primary_card.get("_spec", {})
        accel_id = self.primary_card.get("_accel_id", "")
        ne = spec.get("nvidia_equivalent")
        # NVIDIA 自家卡：spec 不需要显式 nvidia_equivalent，自动对标 spec.match 自身（perf_ratio=1.0）
        if not ne and accel_id == "nvidia":
            match = spec.get("match", "")
            if match:
                ne = {"card": match, "perf_ratio": 1.0}
        if not ne:
            return None
        bench_root = self.profile.get("nvidia_reference_benchmarks", {})
        cards = bench_root.get("cards", {})
        target_card_raw = ne.get("card", "A100-80GB-SXM")
        a100 = cards.get("A100-80GB-SXM", {})
        # 找 cards 表对应 key（容忍 hyphen / 空格 / 后缀差异）
        resolved = self._resolve_cards_key(target_card_raw, cards)
        target = cards.get(resolved, {}) if resolved else {}
        target_card = resolved or target_card_raw
        # single_node_8 现在覆盖 2-8 卡。卡数 <8 时按 actual/8 线性缩放基准
        lookup_id = scenario_id
        scale_to_actual = 1.0
        if scenario_id == "single_node_8" and 0 < actual_cards < 8:
            scale_to_actual = max(0.2, actual_cards / 8.0)
        a100_scenario = a100.get(lookup_id, {})
        tier = self._active_param_tier(active_b)
        a100_tier = a100_scenario.get(tier)
        # 若当前 tier 在该场景下无配置，沿就近档位回退
        if not a100_tier:
            fallback_order = ["tiny", "small", "medium", "large"]
            try:
                i = fallback_order.index(tier)
            except ValueError:
                i = 0
            # 先向上（大→小）再向下（小→大）回退，避免越界用反向切片陷阱
            candidates = list(reversed(fallback_order[:i])) + fallback_order[i+1:]
            for alt in candidates:
                if a100_scenario.get(alt):
                    a100_tier = a100_scenario[alt]
                    break
        if not a100_tier:
            return None
        if target_card == "A100-80GB-SXM":
            base = {
                "single_tps": list(a100_tier["single_tps"]),
                "total_tps":  list(a100_tier["total_tps"]),
                "ttft_ms":    list(a100_tier["ttft_ms"]),
            }
        else:
            mult = target.get("vs_a100_multiplier", 1.0)
            base = {
                "single_tps": [int(a100_tier["single_tps"][0] * mult),
                               int(a100_tier["single_tps"][1] * mult)],
                "total_tps":  [int(a100_tier["total_tps"][0]  * mult),
                               int(a100_tier["total_tps"][1]  * mult)],
                "ttft_ms":    [max(20, int(a100_tier["ttft_ms"][0] / mult)),
                               max(30, int(a100_tier["ttft_ms"][1] / mult))],
            }
        # single_node_small 按实际卡数线性缩放（单流TPS不变，总吞吐和 TTFT 按比例）
        if scale_to_actual != 1.0:
            base["total_tps"] = [int(base["total_tps"][0] * scale_to_actual),
                                 int(base["total_tps"][1] * scale_to_actual)]
        return base

    def _estimate_perf(self, scenario_id: str, model: Dict, precision: str,
                       total_gb: float, weight_gb: float,
                       actual_cards: int = 0) -> Dict[str, str]:
        """基于 NVIDIA 基准 × 对标系数 + INT4 加速 + 显存约束并发，估算各项指标。
        返回字段：单流TPS / 总吞吐 / TTFT / 并发(估)。
        """
        active_str = model.get("active", "10B").replace("~", "").upper()
        m = re.search(r"([\d.]+)", active_str)
        active_b = float(m.group(1)) if m else 10.0

        spec = self.primary_card.get("_spec", {}) if self.primary_card else {}
        ne = spec.get("nvidia_equivalent", {})
        perf_ratio = float(ne.get("perf_ratio", 0.5))

        bench = self._lookup_nvidia_perf(scenario_id, active_b, actual_cards)
        # 没有 NVIDIA 基准时给保守默认值（避免给出误导性的高估）
        if bench is None:
            tps_lo, tps_hi = 8, 18
            tot_lo, tot_hi = 80, 200
            tft_lo, tft_hi = 300, 800
        else:
            tps_lo = max(1, int(bench["single_tps"][0] * perf_ratio))
            tps_hi = max(tps_lo + 2, int(bench["single_tps"][1] * perf_ratio))
            tot_lo = max(10, int(bench["total_tps"][0] * perf_ratio))
            tot_hi = max(tot_lo + 10, int(bench["total_tps"][1] * perf_ratio))
            # TTFT 与算力反相关：性能系数越低，TTFT 越高
            inv = 1.0 / max(perf_ratio, 0.1)
            tft_lo = max(20, int(bench["ttft_ms"][0] * inv))
            tft_hi = max(tft_lo + 30, int(bench["ttft_ms"][1] * inv))

        # INT4 量化在 generation 阶段提速约 30-40%，但 prefill/TTFT 加速有限
        if precision.startswith("INT"):
            tps_lo = int(tps_lo * 1.30)
            tps_hi = int(tps_hi * 1.35)
            tot_lo = int(tot_lo * 1.20)
            tot_hi = int(tot_hi * 1.25)
            tft_lo = int(tft_lo * 0.90)
            tft_hi = int(tft_hi * 0.95)

        # 并发数受 KV Cache 显存约束（每并发约 1 GB KV，长上下文+大模型）
        kv_per_conc_gb = 1.0 if active_b > 20 else 0.6
        kv_budget = max(0.5, total_gb - weight_gb)
        max_conc_by_mem = int(kv_budget / kv_per_conc_gb)
        # 同时受 总吞吐/单流TPS 约束
        max_conc_by_tps = max(2, int(tot_hi / max(tps_lo, 1)))
        # 取两者较小值作为合理上限
        conc_high = max(2, min(max_conc_by_mem, max_conc_by_tps))
        conc_low = max(1, conc_high // 4)

        return {
            "单流 TPS":  f"{tps_lo}-{tps_hi} tok/s",
            "总吞吐":    f"{tot_lo}-{tot_hi} tok/s",
            "TTFT":      f"{tft_lo}-{tft_hi} ms",
            "并发(估)":  f"{conc_low}-{conc_high}",
        }

    def _parse_rdma_ibstatus(self, ibstatus_text: str) -> List[Dict]:
        """解析ibstatus输出，提取RDMA链路状态信息。
        ibstatus块格式:
            Infiniband device 'mlx5_20' port 1 status:
                state:    4: ACTIVE
                rate:     400 Gb/sec (4X NDR)
                link_layer: Ethernet
        """
        if not ibstatus_text or ibstatus_text == "未检测":
            return []
        results = []
        current: Dict[str, str] = {}
        # 按"Infiniband device"为块分隔
        for line in ibstatus_text.split("\n"):
            line = line.rstrip()
            stripped = line.strip()
            if not stripped:
                continue
            # 新设备块开始
            m = re.search(r"Infiniband device\s+'([^']+)'", stripped)
            if m:
                if current.get("设备"):
                    results.append(current)
                current = {"设备": m.group(1), "状态": "—", "速率": "—", "链路层": "—"}
                continue
            if not current:
                continue
            # 字段提取
            if stripped.startswith("state:"):
                # "state:    4: ACTIVE" → ACTIVE
                tail = stripped.split(":", 1)[1].strip()
                # 取最后一个token（如 ACTIVE/DOWN）
                tokens = tail.replace(":", " ").split()
                if tokens:
                    current["状态"] = tokens[-1]
            elif stripped.startswith("rate:"):
                # "rate:    400 Gb/sec (4X NDR)" → 400 Gb/sec (4X NDR)
                current["速率"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("link_layer:"):
                current["链路层"] = stripped.split(":", 1)[1].strip()
        if current.get("设备"):
            results.append(current)
        return results if results else [{"设备": "未检测", "状态": "—", "速率": "—", "链路层": "—"}]

    def _parse_rdma_devinfo(self, devinfo_text: str) -> List[Dict]:
        """解析ibv_devinfo输出，提取RDMA设备信息。
        ibv_devinfo块格式:
            hca_id:    mlx5_20
                    state:        PORT_ACTIVE (4)
                    active_mtu:   4096 (5)
        """
        if not devinfo_text or devinfo_text == "未检测":
            return []
        results = []
        current: Dict[str, str] = {}
        for line in devinfo_text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("hca_id:"):
                if current.get("HCA名称"):
                    results.append(current)
                current = {
                    "HCA名称": stripped.split(":", 1)[1].strip(),
                    "MTU": "—",
                    "状态": "—",
                }
                continue
            if not current:
                continue
            if stripped.startswith("active_mtu:"):
                current["MTU"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("state:"):
                tail = stripped.split(":", 1)[1].strip()
                # 取 "PORT_ACTIVE (4)" 中的状态部分
                if "PORT_ACTIVE" in tail:
                    current["状态"] = "ACTIVE"
                elif "PORT_DOWN" in tail:
                    current["状态"] = "DOWN"
                else:
                    current["状态"] = tail.split()[0] if tail else "—"
        if current.get("HCA名称"):
            results.append(current)
        return results if results else [{"HCA名称": "未检测", "MTU": "—", "状态": "—"}]

    def _parse_rdma_gids(self, gids_text: str) -> List[Dict]:
        """解析RoCE GID表。
        show_gids表格列序: DEV PORT INDEX GID [IPv4] VER DEV
        只显示带IPv4地址的行（真正用于RoCE通信的GID）。
        """
        if not gids_text or gids_text == "未检测":
            return []
        results = []
        for line in gids_text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            # 跳过表头和分隔线
            if stripped.startswith("DEV") or stripped.startswith("---") or stripped.startswith("n_gids"):
                continue
            parts = stripped.split()
            if len(parts) < 5:
                continue
            dev = parts[0]
            port = parts[1]
            index = parts[2]
            gid = parts[3]
            # 检查是否有IPv4字段：判断剩余的token里是否有点分十进制
            ipv4 = "—"
            ver = "—"
            netdev = "—"
            for p in parts[4:]:
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", p):
                    ipv4 = p
                elif p in ("v1", "v2"):
                    ver = p
                elif p.startswith(("eth", "bond", "ens", "enp")):
                    netdev = p
            # 只保留有IPv4地址的行（RoCEv2 GID）
            if ipv4 == "—":
                continue
            results.append({
                "设备": dev,
                "端口": port,
                "索引": index,
                "IPv4": ipv4,
                "版本": ver,
                "网卡": netdev,
            })
        return results if results else []

    # ── Markdown 渲染 ──
    def to_markdown(self) -> str:
        ana = self.analyze()
        dep = self.recommend()
        mods = self.evaluate_models()
        scns = self.perf_scenarios()

        os_i = self.sw.get("操作系统", {})
        cpu  = self.hw.get("CPU", {})
        mem  = self.hw.get("内存", {})
        sto  = self.hw.get("存储", {})
        net  = self.hw.get("网络", {})
        accel = self.hw.get("算力卡", {})
        drv  = self.sw.get("驱动程序", {})
        cluster = self.sw.get("RDMA与集群", {})
        ctr  = self.sw.get("容器与K8s", {})
        ml   = self.sw.get("ML推理框架", {})

        L: List[str] = []
        a = L.append

        a("# 🖥️ 服务器推理能力评估报告")
        a("")
        a(f"> **主机:** `{self.hostname}`  ")
        a(f"> **评估时间:** `{self.ts}`  ")
        a(f"> **工具版本:** Server Inspector **{self.tool_version}**（配置驱动）")
        a("")
        a("---")
        a("")

        a("## 📋 一、环境总览")
        a("")
        if ana["问题"]:
            a("### ❌ 需修复问题")
            for i in ana["问题"]:
                a(f"- {i}")
            a("")
        if ana["警告"]:
            a("### ⚠️ 警告")
            for w in ana["警告"]:
                a(f"- {w}")
            a("")
        if ana["正常"]:
            a("### ✅ 正常项")
            for g in ana["正常"]:
                a(f"- {g}")
            a("")

        a("### 配置速览")
        a("")
        a("| 类别 | 配置 |")
        a("|:----|:----|")
        a(f"| 🔹 **CPU** | {cpu.get('厂商','?')} — {cpu.get('型号','?')[:55]} |")
        a(f"| 🔹 **核数/线程** | {cpu.get('每CPU核数','?')} 核 × {cpu.get('物理CPU数','?')} 路 / "
          f"{cpu.get('总线程数','?')} 线程 |")
        a(f"| 🔹 **向量指令** | {cpu.get('向量指令集','?')} |")
        mem_extra = ""
        if mem.get("内存类型", "未知") != "未知":
            mem_extra = f" ({mem.get('内存类型','?')} @ {mem.get('内存频率','?')})"
        a(f"| 🔹 **系统内存** | {mem.get('总容量','?')}{mem_extra} |")

        if self.primary_card:
            a(f"| 🔹 **算力卡** | {self.primary_card['_display_name']} "
              f"{self.primary_card.get('卡型号','?')} × {self.primary_card['_card_count']} |")
            a(f"| 🔹 **单卡显存** | {self.primary_card['_single_vram_mib']/1024:.0f} GB |")
        else:
            a("| 🔹 **算力卡** | 未检测到 |")
        a(f"| 🔹 **总显存** | **{self.total_vram_gb:.0f} GB** |")
        a(f"| 🔹 **操作系统** | {os_i.get('发行版','?')} |")
        a(f"| 🔹 **建议部署** | **{dep['部署方式']}** |")
        a("")

        a("## 🔧 二、硬件配置详情")
        a("")
        a("### 2.1 CPU")
        a("")
        a("| 参数 | 值 |")
        a("|:----|:----|")
        for k in ["厂商", "型号", "架构", "物理CPU数", "每CPU核数", "总线程数",
                  "超线程", "最大主频", "向量指令集", "NUMA节点数",
                  "L1d缓存", "L1i缓存", "L2缓存", "L3缓存", "虚拟化"]:
            v = cpu.get(k, "—")
            a(f"| {k} | {v} |")
        a("")
        if cpu.get("NUMA拓扑"):
            a("**NUMA 拓扑详情：**")
            a("")
            a("```")
            a(cpu["NUMA拓扑"])
            a("```")
            a("")

        a("### 2.2 内存")
        a("")
        a("| 参数 | 值 |")
        a("|:----|:----|")
        for k in ["总容量", "已使用", "可用", "Swap总量",
                  "内存类型", "内存频率", "内存厂商",
                  "内存条总插槽", "内存条已使用", "内存条详情",
                  "HugePages_Total", "Hugepagesize"]:
            if k in mem:
                a(f"| {k} | {mem[k]} |")
        a("")

        a("### 2.3 存储")
        a("")
        a("**块设备：**")
        a("```")
        a(sto.get("块设备列表", "无"))
        a("```")
        a("")
        a("**文件系统挂载：**")
        a("```")
        a(sto.get("文件系统挂载", "无"))
        a("```")
        for key in ["NVMe设备", "多路径存储", "软件RAID"]:
            if sto.get(key):
                a("")
                a(f"**{key}：**")
                a("```")
                a(str(sto[key])[:800])
                a("```")
        shared = {k: v for k, v in sto.items() if k.startswith("共享存储_")}
        if shared:
            a("")
            a("**共享存储：**")
            a("")
            a("| 类型 | 挂载详情 |")
            a("|:----|:----|")
            for k, v in shared.items():
                label = k.replace("共享存储_", "")
                v_short = str(v).split("\n")[0][:120]
                a(f"| {label} | `{v_short}` |")
            a("")
        if sto.get("磁盘读速参考"):
            a("**磁盘顺序读速（hdparm -t）：**")
            a("")
            a("| 设备 | 速率 |")
            a("|:----|:----|")
            for dev, spd in sto["磁盘读速参考"].items():
                a(f"| `{dev}` | {spd} |")
            a("")
        if sto.get("SMART健康"):
            a("**SMART 健康状态：**")
            a("")
            a("| 设备 | 状态 |")
            a("|:----|:----|")
            for dev, st in sto["SMART健康"].items():
                a(f"| `{dev}` | {st[:80]} |")
            a("")

        a("### 2.4 网络")
        a("")
        a("**接口概览：**")
        a("```")
        a(net.get("网络接口概览", "无"))
        a("```")
        a("")
        if net.get("网卡速率"):
            a("**网卡速率表：**")
            a("")
            a("| 接口 | Speed | Duplex | Port | Link |")
            a("|:----|:----|:----|:----|:----:|")
            for iface, d in net["网卡速率"].items():
                a(
                    f"| `{iface}` | {d.get('Speed','—')} | "
                    f"{d.get('Duplex','—')} | {d.get('Port','—')} | "
                    f"{d.get('Link detected','—')} |"
                )
            a("")
        if net.get("PCIe网络设备"):
            a("**PCIe 网络设备：**")
            a("```")
            a(net["PCIe网络设备"])
            a("```")
            a("")
        # RDMA链路状态
        if net.get("RDMA链路状态(ibstatus)"):
            a("**RDMA 链路状态 (ibstatus)：**")
            a("")
            rdma_status = self._parse_rdma_ibstatus(net["RDMA链路状态(ibstatus)"])
            if rdma_status:
                a("| 设备 | 状态 | 速率 | 链路层 |")
                a("|:----|:----|:----|:----|")
                for item in rdma_status:
                    a(f"| {item.get('设备','—')} | {item.get('状态','—')} | {item.get('速率','—')} | {item.get('链路层','—')} |")
                a("")

        # RDMA设备详情
        if net.get("RDMA设备详情(ibv_devinfo)"):
            a("**RDMA 设备详情 (ibv_devinfo)：**")
            a("")
            rdma_devinfo = self._parse_rdma_devinfo(net["RDMA设备详情(ibv_devinfo)"])
            if rdma_devinfo:
                a("| HCA 名称 | 状态 | MTU |")
                a("|:----|:----|:----|")
                for item in rdma_devinfo:
                    a(f"| {item.get('HCA名称','—')} | {item.get('状态','—')} | {item.get('MTU','—')} |")
                a("")

        # RoCE GID表（只显示带IPv4的RoCEv2 GID）
        if net.get("RoCE GID表"):
            a("**RoCE GID 表（仅展示带 IPv4 的 RoCE v2 条目）：**")
            a("")
            rdma_gids = self._parse_rdma_gids(net["RoCE GID表"])
            if rdma_gids:
                a("| 设备 | 端口 | 索引 | IPv4 | 版本 | 网卡 |")
                a("|:----|:----|:----|:----|:----|:----|")
                for item in rdma_gids:
                    a(f"| {item.get('设备','—')} | {item.get('端口','—')} | {item.get('索引','—')} | {item.get('IPv4','—')} | {item.get('版本','—')} | {item.get('网卡','—')} |")
                a("")
            else:
                a("*未检测到带 IPv4 地址的 RoCE GID 条目*")
                a("")
        if net.get("FC Host设备"):
            a("**FC HBA：**")
            a(f"`{net['FC Host设备']}`")
            a("")

        a("### 2.5 算力卡")
        a("")
        a(f"**检测结果：** {self.accel_type}")
        a("")
        for card in self.detected_cards:
            a(f"#### {card['_display_name']}")
            a("")
            a("| 参数 | 值 |")
            a("|:----|:----|")
            # 这里只展示"检测到的拓扑/形态"信息，详细算力/显存规格留给下面的
            # "性能及规格参考"表，避免与之重复
            for k in ["卡型号", "卡数", "单卡显存", "总显存", "PCIe", "互联", "TDP"]:
                if k in card:
                    bold = k in ("卡型号", "卡数", "单卡显存", "总显存")
                    val = card[k]
                    a(f"| {k} | **{val}** |" if bold else f"| {k} | {val} |")
            a(f"| 推荐框架 | {', '.join(card.get('_frameworks', []))} |")
            a("")
            if card.get("PCIe设备列表") and card["PCIe设备列表"] != "未检测":
                a("**PCIe 设备列表：**")
                a("```")
                a(card["PCIe设备列表"])
                a("```")
                a("")

        # 其他兜底设备
        for k, v in accel.items():
            if k.startswith("_") or k in [c["_accel_id"] for c in self.detected_cards]:
                continue
            a(f"**{k}：**")
            a("```")
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    a(f"[{k2}] {str(v2)[:300]}")
            else:
                a(str(v)[:600])
            a("```")
            a("")

        a("## 💻 三、系统软件环境")
        a("")
        a("### 3.1 操作系统")
        a("")
        a("| 项 | 值 |")
        a("|:----|:----|")
        for k in ["主机名", "发行版", "版本号", "内核版本", "运行时长", "SELinux", "AppArmor"]:
            if k in os_i:
                v = str(os_i[k])[:120]
                a(f"| {k} | `{v}` |")
        a("")
        if os_i.get("关键内核参数"):
            a("**关键内核参数：**")
            a("```")
            a(os_i["关键内核参数"])
            a("```")
            a("")

        a("### 3.2 驱动程序")
        a("")
        if drv:
            a("| 项 | 值 |")
            a("|:----|:----|")
            for k, v in drv.items():
                if isinstance(v, dict):
                    for k2, v2 in v.items():
                        a(f"| {k} / {k2} | `{str(v2)[:100]}` |")
                else:
                    val = str(v).replace("\n", " ; ")[:200]
                    a(f"| {k} | `{val}` |")
            a("")
        else:
            a("*未检测到专用驱动*")
            a("")

        if cluster:
            a("### 3.3 RDMA / MPI / 集群工具链")
            a("")
            rdma_set = cluster.get("RDMA 工具集", {})
            if rdma_set:
                a("**RDMA 工具集：**")
                a("")
                a("| 类型 | 可用工具 |")
                a("|:----|:----|")
                for k, v in rdma_set.items():
                    a(f"| {k} | `{v}` |")
                a("")
            if cluster.get("Mellanox 管理工具"):
                a(f"**Mellanox 管理工具：** `{cluster['Mellanox 管理工具']}`")
                a("")
            if cluster.get("MPI 实现"):
                a("**MPI 实现：**")
                for v in cluster["MPI 实现"]:
                    a(f"- {v}")
                a("")
            ccl = cluster.get("集合通信库 / GPUDirect", {})
            if ccl:
                a("**集合通信库 / GPUDirect：**")
                a("")
                a("| 库 | 状态 |")
                a("|:----|:----|")
                for k, v in ccl.items():
                    a(f"| {k} | `{str(v)[:120]}` |")
                a("")
            sched = cluster.get("集群调度器", {})
            if sched:
                a("**集群调度器：**")
                a("")
                a("| 调度器 | 版本 |")
                a("|:----|:----|")
                for k, v in sched.items():
                    a(f"| {k} | `{v}` |")
                a("")

        if ctr:
            a("### 3.4 容器 / Kubernetes 生态")
            a("")
            if ctr.get("Docker"):
                a(f"**Docker：** `{ctr['Docker'].get('版本','—')}`")
                if ctr["Docker"].get("关键配置"):
                    a("")
                    a("```")
                    a(ctr["Docker"]["关键配置"])
                    a("```")
                a("")
            for k in ["containerd", "Podman", "nerdctl", "crictl"]:
                if k in ctr:
                    a(f"**{k}：** `{ctr[k]}`")
                    a("")
            if ctr.get("GPU 容器工具"):
                a("**GPU 容器工具：**")
                a("")
                a("| 工具 | 版本 |")
                a("|:----|:----|")
                for k, v in ctr["GPU 容器工具"].items():
                    a(f"| {k} | `{str(v)[:100]}` |")
                a("")
            if ctr.get("Kubernetes 工具链"):
                a("**Kubernetes 工具链：**")
                a("")
                a("| 工具 | 版本 |")
                a("|:----|:----|")
                for k, v in ctr["Kubernetes 工具链"].items():
                    a(f"| {k} | `{str(v)[:100]}` |")
                a("")
            if ctr.get("K8s 节点状态"):
                a("**K8s 节点状态：**")
                a("```")
                a(ctr["K8s 节点状态"])
                a("```")
                a("")
            a(f"**当前脚本运行环境：** {ctr.get('当前脚本运行环境','—')}")
            a("")

        a("### 3.5 ML 推理框架")
        a("")
        a("| 框架 | 状态 |")
        a("|:----|:----|")
        for k, v in ml.items():
            if k != "相关 pip 包":
                v_safe = str(v).replace("|", "·")
                a(f"| {k} | `{v_safe}` |")
        a("")
        if ml.get("相关 pip 包"):
            a("**已安装相关 pip 包：**")
            a("```")
            a(ml["相关 pip 包"])
            a("```")
            a("")
        if ml.get("运行时可见性"):
            a("**运行时可见性：**")
            a("")
            a("| 项 | 值 |")
            a("|:----|:----|")
            for k, v in ml["运行时可见性"].items():
                a(f"| {k} | {v} |")
            a("")

        a("## 🚀 四、部署方案建议")
        a("")
        a("| 建议项 | 值 |")
        a("|:----|:----|")
        a(f"| **建议部署方式** | {dep['部署方式']} |")
        a(f"| **张量并行度 (TP)** | {dep['张量并行度']} |")
        a(f"| **单卡显存** | {dep['单卡显存']} |")
        a(f"| **总显存** | **{dep['总显存']}** |")
        a("")
        a("**推荐推理框架（按优先级）：**")
        for i, fw in enumerate(dep["推荐框架"], 1):
            a(f"{i}. {fw}")
        a("")

        if self.primary_card:
            spec = self.primary_card["_spec"]
            a(f"### {self.primary_card['_display_name']} 性能及规格参考")
            a("")
            a("| 参数 | 值 |")
            a("|:----|:----|")
            a(f"| 型号 | {self.primary_card.get('卡型号','?')} |")

            # 算力字段：FP32/FP16 总显示（不支持/缺数据时分别给标识）；
            # BF16/INT8/INT4 按是否支持展示。
            def _fmt_tflops(v, unit="TFLOPS"):
                if v is None or v == "":
                    return "暂无数据"
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    return str(v)
                if fv == 0:
                    return "不支持"
                return f"{fv:g} {unit}"

            a(f"| FP32 算力 | {_fmt_tflops(spec.get('fp32_tflops'))} |")
            a(f"| FP16 算力 | {_fmt_tflops(spec.get('fp16_tflops'))} |")
            if "bf16_tflops" in spec:
                a(f"| BF16 算力 | {_fmt_tflops(spec.get('bf16_tflops'))} |")
            if "int8_tops" in spec:
                a(f"| INT8 算力 | {_fmt_tflops(spec.get('int8_tops'), 'TOPS')} |")
            if "int4_tops" in spec:
                a(f"| INT4 算力 | {_fmt_tflops(spec.get('int4_tops'), 'TOPS')} |")
            # 显存容量：先展示标称值；若实测值与标称差 ≥ 1 GB（魔改卡 / SR-IOV /
            # vGPU 等场景），追加"（实测 X GB）"提示
            spec_hbm = spec.get("hbm_gb")
            actual_mib = self.primary_card.get("_single_vram_mib", 0)
            mem_cell = f"{spec_hbm} GB" if spec_hbm is not None else "?"
            if spec_hbm and actual_mib:
                actual_gb = actual_mib / 1024
                if abs(actual_gb - spec_hbm) >= 1:
                    mem_cell = f"{spec_hbm} GB（实测 {actual_gb:.0f} GB）"
            a(f"| 显存容量 | {mem_cell} |")
            a(f"| 显存带宽 | {spec.get('hbm_bw_tbps','?')} TB/s |")
            if spec.get("memory_type"):
                a(f"| 显存类型 | {spec['memory_type']} |")
            a(f"| 功耗 (TDP) | {spec.get('tdp_w','?')} W |")
            a(f"| PCIe / 形态 | {spec.get('pcie','?')} |")
            a(f"| 互联 | {spec.get('interconnect','?')} |")
            a("")

        a("## 🤖 五、模型兼容性评估")
        a("")
        a("> 基于当前硬件配置对 **2026 年主流开源大语言模型** 的部署可行性评估")
        a("> v0.10 起不再按“总显存是否大于权重”直接判定，而是按单卡可用显存、运行时开销、")
        a("> 目标上下文下的 KV Cache 预算粗估部署可行性；结果适合选型期预判，正式上线仍需实测校准。")
        a("")
        a("### 5.1 部署摘要")
        a("")
        a("| 模型 | 建议精度 | 推荐并行 | 理论最大并发 | 框架适配 | 部署状态 |")
        a("|:----|:----:|:----:|:----:|:----|:----|")

        def compact_status(text: str) -> str:
            if text.startswith("✅"):
                return "✅ 可部署"
            if "KV" in text or text.startswith("❌"):
                return "❌ 目标上下文不足"
            if text.startswith("⚠️"):
                return "⚠️ 需适配验证"
            return text or "—"

        for m in mods:
            a(
                f"| **{m['name']}** | {m['建议精度']} | {m.get('推荐并行','—')} "
                f"| {m.get('理论最大并发','—')} | {m['框架适配']} | {compact_status(m['状态'])} |"
            )
        a("")
        a("### 5.2 详细规格（宽表）")
        a("")
        a("> 下表字段较多，HTML 版会自动提供横向滚动；Markdown 版建议在支持表格滚动的预览器中查看。")
        a("")
        a("| 模型 | 厂商 | 参数 | 激活 | 上下文 | BF16 显存 | INT8 显存 | INT4 显存 | 建议精度 | 推荐并行 | 理论最大并发 | 框架适配 | 说明 | 状态 |")
        a("|:----|:----|:----|:----|:----:|----:|----:|----:|:----:|:----:|:----:|:----|:----|:----|")
        for m in mods:
            a(
                f"| **{m['name']}** | {m['vendor']} | {m['params']} | {m['active']} "
                f"| {m['context']} | {m['fp16_gb']} GB | {m['int8_gb']} GB | {m['int4_gb']} GB "
                f"| {m['建议精度']} | {m.get('推荐并行','—')} | {m.get('理论最大并发','—')} "
                f"| {m['框架适配']} | {m.get('notes','—')} | {m['状态']} |"
            )
        a("")

        a("## 📊 六、性能预估")
        a("")
        a("> 本章给出的是 **硬件物理边界估算**，不是框架实测值。")
        a("> Decode 上限偏带宽约束，Prefill 上限偏算力约束；国产卡实际表现通常还会受到"
          "算子库、通信库、容器可见卡数和框架适配度影响。")
        a("")
        if not scns:
            a("*未匹配到适用场景（典型原因：单卡显存不足以承载评估清单中的任一模型，或卡型未在数据库中收录）。*")
            a("")
        else:
            for idx, s in enumerate(scns, 1):
                a(f"### 6.{idx} {s['场景']}")
                a("")
                a(f"**硬件配置：** {s['硬件']}")
                a("")
                if s.get("口径"):
                    a(f"**估算口径：** {s['口径']}")
                    a("")
                if s.get("通信"):
                    a(f"**通信架构：** {s['通信']}")
                    a("")
                if s.get("前置条件"):
                    a("**前置条件：**")
                    for p in s["前置条件"]:
                        a(f"- {p}")
                    a("")
                a("**性能边界估算：**")
                a("")
                a("| 模型 | 推荐精度 | 参考并行 | Decode 上限 | Prefill 上限 | 理论最大并发 | 置信度 | 说明 |")
                a("|:----|:----:|:----:|:----:|:----:|:----:|:----:|:----|")
                for mp in s["适用模型"]:
                    a(
                        f"| **{mp['模型']}** | {mp.get('推荐精度','—')} | "
                        f"{mp.get('参考并行','—')} | {mp.get('Decode 上限','—')} | "
                        f"{mp.get('Prefill 上限','—')} | {mp.get('理论最大并发','—')} | "
                        f"{mp.get('置信度','—')} | {mp.get('说明','—')} |"
                    )
                a("")
                if s.get("推理框架"):
                    a(f"**推荐框架：** {s['推理框架']}")
                    a("")
                if s.get("建议用途"):
                    a(f"**建议用途：** {s['建议用途']}")
                    a("")

            # 性能指标说明：仅在有场景输出时展示
            a("### 指标说明")
            a("")
            a("| 指标 | 全称 | 说明 |")
            a("|:----|:----|:----|")
            a("| Decode 上限 | Decode Throughput Upper Bound | 按显存带宽和每卡权重装载量粗估的生成阶段上限 |")
            a("| Prefill 上限 | Prefill Throughput Upper Bound | 按算力和激活参数量粗估的长上下文预填充上限 |")
            a("| 理论最大并发 | Max Concurrent Sequences | 在当前上下文和显存预算模型下粗估的并发上限 |")
            a("| 置信度 | Confidence | 对该卡型和后端估算可靠性的主观等级，低表示需强依赖实测 |")
            a("")

        bench_cmds = self._benchmark_commands()
        if bench_cmds:
            a("## 🧪 实测校准建议")
            a("")
            a("> 以下命令用于在目标环境做保守盲测。建议先从 `--gpu-memory-utilization 0.5` 和 "
              "`--max-num-seqs 1` 起步，确认可启动后再逐步提高。")
            a("")
            for item in bench_cmds:
                a(f"**{item['模型']}** 参考方案：{item['建议']}")
                a("")
                a("```bash")
                a(item["命令"])
                a("```")
                a("")
            a("> 观察日志里的可用 KV Cache、Profile Peak 和 OOM 信息，再回头校准平台系数；"
              "国产卡后端差异较大，最终部署请以实测结论为准。")
            a("")

        # 七、新硬件贡献提示（仅在检测到未收录硬件时显示）
        if self.unknown_items:
            a("## 🆕 七、新硬件贡献提示")
            a("")
            a(f"本次扫描检测到 **{len(self.unknown_items)}** 项数据库未收录的硬件：")
            a("")
            for i, item in enumerate(self.unknown_items, 1):
                a(f"{i}. **[{item['type'].upper()}]** {item['summary']}")
            a("")
            if self.contribution_path:
                a("已为您生成贡献数据文件：")
                a("")
                a(f"```\n{self.contribution_path}\n```")
                a("")
            contrib_cfg = self.profile.get("contribution", {})
            issue_url = contrib_cfg.get("issue_url", "")
            email = contrib_cfg.get("contact_email", "")
            if issue_url or email:
                a("如愿意贡献这份硬件数据帮助完善项目数据库，请通过以下任一方式发送给维护者：")
                a("")
                if issue_url:
                    a(f"- **Gitee Issue（推荐）：** <{issue_url}>")
                    a("  在 Issue 描述里附上 JSON 内容即可")
                if email:
                    a(f"- **邮箱：** {email}")
                    a("  将 JSON 文件作为附件发送")
                a("")
            a("> **隐私说明**：贡献文件仅包含硬件配置（CPU/GPU 型号、显存、驱动版本、PCIe 拓扑等），"
              "不含用户名、密码、公网 IP、文件系统路径等敏感信息。"
              "您可在发送前打开 JSON 查看完整内容，确认后再发送。")
            a("")

        cite = self.profile.get("citation", {})
        if cite.get("enabled") and cite.get("bibtex"):
            a("## 📚 引用")
            a("")
            a(cite.get(
                "intro",
                "如果您在研究、选型、平台推广或技术方案评估中使用了 Server Inspector 生成的数据或报告，请引用："
            ))
            a("")
            a("```bibtex")
            a(cite["bibtex"].strip())
            a("```")
            a("")

        a("---")
        a("")
        a('<div class="notice" markdown="1">')
        a('**特别提醒** 本报告中的性能章节为硬件边界估算，不等同于框架实测。'
          '实际表现会受到模型结构、推理框架、量化方式、容器可见卡数、互联拓扑和厂商算子库成熟度影响，'
          '正式部署请务必以目标环境的启动日志和压测结果为准。')
        a("</div>")
        a("")
        a(f"*报告生成: {self.ts} | Server Inspector {self.tool_version} | 配置文件驱动 | 主机: `{self.hostname}`*")
        return "\n".join(L)

    # ── HTML 渲染 ──
    def to_html(self, md: str) -> str:
        css = """
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',
     'Hiragino Sans GB','Microsoft YaHei',sans-serif;
     background:linear-gradient(135deg,#f0f4f8 0%,#e8eef5 100%);
     color:#1f2937;margin:0;padding:24px;line-height:1.65}
.wrap{max-width:1320px;margin:0 auto;background:#fff;border-radius:14px;
      padding:48px 56px;box-shadow:0 8px 36px rgba(15,23,42,.08)}
h1{color:#0f172a;border-bottom:4px solid;border-image:linear-gradient(90deg,#3b82f6,#8b5cf6) 1;
   padding-bottom:14px;font-size:1.95em;margin-top:8px;font-weight:700}
h2{color:#1e293b;border-left:5px solid #3b82f6;padding-left:14px;
   margin-top:42px;font-size:1.35em;font-weight:600;background:linear-gradient(90deg,#eff6ff 0%,transparent 60%);padding-top:8px;padding-bottom:8px;border-radius:0 6px 6px 0}
h3{color:#0f172a;font-size:1.1em;margin-top:28px;font-weight:600;
   border-bottom:1px dashed #cbd5e1;padding-bottom:6px}
h4{color:#374151;font-size:1em;margin-top:18px}
p{margin:8px 0}
.table-wrap{overflow-x:auto;margin:14px 0 22px;border:1px solid #e5e7eb;border-radius:10px;
            box-shadow:0 1px 3px rgba(0,0,0,.04);background:#fff}
table{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%;margin:0;
      font-size:.92em;border:none;border-radius:0;overflow:visible;box-shadow:none}
thead tr{background:linear-gradient(90deg,#3b82f6,#6366f1);color:#fff}
th{padding:11px 16px;text-align:left;font-weight:600;letter-spacing:.3px;
   white-space:nowrap;border:none}
tbody tr{transition:background .15s}
tbody tr:nth-child(even){background:#f9fafb}
tbody tr:hover{background:#eff6ff}
td{padding:9px 16px;border-bottom:1px solid #f1f5f9;vertical-align:top}
td:last-child{min-width:280px}
tbody tr:last-child td{border-bottom:none}
td strong{color:#1e40af}
pre{background:#1e293b;color:#e2e8f0;padding:16px 20px;border-radius:8px;
    overflow-x:auto;font-size:.86em;line-height:1.55;
    font-family:'JetBrains Mono','Cascadia Code','Consolas',monospace;
    border-left:3px solid #3b82f6}
code{background:#eff6ff;color:#1e40af;padding:2px 7px;border-radius:4px;
     font-size:.88em;font-family:'JetBrains Mono','Consolas',monospace;
     border:1px solid #dbeafe}
pre code{background:none;color:inherit;padding:0;border:none}
blockquote{border-left:4px solid #3b82f6;margin:14px 0;padding:12px 20px;
           background:linear-gradient(90deg,#eff6ff,#f8fafc);border-radius:0 8px 8px 0;
           color:#475569;font-style:normal}
blockquote strong{color:#1e40af}
.notice{border:1px solid #f59e0b;background:linear-gradient(135deg,#fffbeb 0%,#fef3c7 100%);
        border-radius:8px;padding:10px 14px;margin:18px 0;font-size:.78em;
        color:#78350f;line-height:1.5}
.notice p{margin:0}
.notice p strong:first-child{display:inline-block;background:#f59e0b;color:#fff;
            padding:1px 8px;border-radius:4px;margin-right:8px;font-size:.95em;
            letter-spacing:.5px}
ul{padding-left:24px}
ol{padding-left:24px}
li{margin:5px 0}
hr{border:none;border-top:1px dashed #cbd5e1;margin:28px 0}
.meta{background:linear-gradient(135deg,#fafbfc 0%,#f3f4f6 100%);
      border:1px solid #e5e7eb;border-radius:10px;
      padding:18px 24px;margin-bottom:24px;font-size:.92em;color:#475569;
      display:flex;gap:32px;flex-wrap:wrap}
.meta-item{display:flex;flex-direction:column;gap:2px}
.meta-label{font-size:.78em;color:#64748b;text-transform:uppercase;letter-spacing:.5px}
.meta-value{font-weight:600;color:#1e293b;font-family:'JetBrains Mono',monospace}
.badge{display:inline-block;padding:2px 9px;border-radius:12px;
       font-size:.82em;font-weight:500;margin-right:4px}
.badge-ok{background:#dcfce7;color:#166534}
.badge-warn{background:#fef3c7;color:#92400e}
.badge-err{background:#fee2e2;color:#991b1b}
a{color:#2563eb;text-decoration:none}
a:hover{text-decoration:underline}
"""
        lines = md.splitlines()
        out: List[str] = []
        in_pre = False
        in_table = False
        in_ul = False
        in_ol = False
        header_done = False
        col_aligns: List[str] = []

        def flush_lists():
            nonlocal in_ul, in_ol
            if in_ul:
                out.append("</ul>"); in_ul = False
            if in_ol:
                out.append("</ol>"); in_ol = False

        def flush_table():
            nonlocal in_table, header_done, col_aligns
            if in_table:
                out.append("</tbody></table></div>")
                in_table = False
                header_done = False
                col_aligns = []

        def md_inline(text: str) -> str:
            text = html_lib.escape(text)
            text = text.replace("✅", '<span class="badge badge-ok">✓</span>')
            text = text.replace("⚠️", '<span class="badge badge-warn">!</span>')
            text = text.replace("❌", '<span class="badge badge-err">×</span>')
            text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
            text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
            text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
            return text

        for line in lines:
            if line.startswith("```"):
                if not in_pre:
                    flush_lists(); flush_table()
                    out.append("<pre><code>")
                    in_pre = True
                else:
                    out.append("</code></pre>")
                    in_pre = False
                continue
            if in_pre:
                out.append(html_lib.escape(line))
                continue

            if line.startswith("|"):
                if not in_table:
                    flush_lists()
                    out.append('<div class="table-wrap"><table>')
                    in_table = True
                    header_done = False
                if re.match(r"^\|[\s\-:|]+\|$", line):
                    col_aligns = []
                    for c in line.split("|")[1:-1]:
                        c = c.strip()
                        if c.startswith(":") and c.endswith(":"):
                            col_aligns.append("center")
                        elif c.endswith(":"):
                            col_aligns.append("right")
                        else:
                            col_aligns.append("left")
                    out.append("<tbody>")
                    header_done = True
                    continue
                cells = [c.strip() for c in line.split("|")[1:-1]]
                if not header_done:
                    out.append("<thead><tr>" +
                               "".join(f"<th>{md_inline(c)}</th>" for c in cells) +
                               "</tr></thead>")
                else:
                    tds = []
                    for i, c in enumerate(cells):
                        align = col_aligns[i] if i < len(col_aligns) else "left"
                        tds.append(f'<td style="text-align:{align}">{md_inline(c)}</td>')
                    out.append("<tr>" + "".join(tds) + "</tr>")
                continue
            else:
                flush_table()

            if line.startswith("#### "):
                flush_lists()
                out.append(f"<h4>{md_inline(line[5:])}</h4>")
            elif line.startswith("### "):
                flush_lists()
                out.append(f"<h3>{md_inline(line[4:])}</h3>")
            elif line.startswith("## "):
                flush_lists()
                out.append(f"<h2>{md_inline(line[3:])}</h2>")
            elif line.startswith("# "):
                flush_lists()
                out.append(f"<h1>{md_inline(line[2:])}</h1>")
            elif line.startswith("> "):
                flush_lists()
                out.append(f"<blockquote>{md_inline(line[2:])}</blockquote>")
            elif line.startswith("- "):
                if not in_ul:
                    if in_ol: out.append("</ol>"); in_ol = False
                    out.append("<ul>"); in_ul = True
                out.append(f"<li>{md_inline(line[2:])}</li>")
            elif re.match(r"^\d+\.\s", line):
                if not in_ol:
                    if in_ul: out.append("</ul>"); in_ul = False
                    out.append("<ol>"); in_ol = True
                content = re.sub(r"^\d+\.\s", "", line)
                out.append(f"<li>{md_inline(content)}</li>")
            elif line.strip() == "---":
                flush_lists()
                out.append("<hr>")
            elif line.strip() == "":
                flush_lists()
            elif re.match(r"^\s*</?(div|section|aside|details|summary)[\s>]", line):
                # 原样透传容器级 HTML 标签（用于免责声明等特殊样式块）
                flush_lists()
                out.append(line)
            else:
                flush_lists()
                out.append(f"<p>{md_inline(line)}</p>")

        flush_lists()
        flush_table()
        if in_pre:
            out.append("</code></pre>")

        body = "\n".join(out)
        primary = self.primary_card["_display_name"] if self.primary_card else "N/A"
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>推理能力评估报告 — {html_lib.escape(self.hostname)}</title>
<style>{css}</style>
</head>
<body>
<div class="wrap">
<div class="meta">
  <div class="meta-item">
    <span class="meta-label">主机</span>
    <span class="meta-value">{html_lib.escape(self.hostname)}</span>
  </div>
  <div class="meta-item">
    <span class="meta-label">评估时间</span>
    <span class="meta-value">{self.ts}</span>
  </div>
  <div class="meta-item">
    <span class="meta-label">工具</span>
    <span class="meta-value">Server Inspector {html_lib.escape(self.tool_version)}</span>
  </div>
  <div class="meta-item">
    <span class="meta-label">算力</span>
    <span class="meta-value">{html_lib.escape(primary)} × {self.accel_count}（{self.total_vram_gb:.0f} GB HBM）</span>
  </div>
</div>
{body}
</div>
</body>
</html>"""


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="服务器推理能力评估工具")
    parser.add_argument("--output-dir", default="./reports",
                        help="报告输出目录 (默认: ./reports)")
    parser.add_argument("--profile", default="profiles.json",
                        help="配置文件路径，自动匹配 .enc / .json (默认: profiles)")
    parser.add_argument("--encode-profile", metavar="INPUT.json",
                        help="将明文 JSON 配置编码为 .enc 文件后退出（维护者使用）")
    args = parser.parse_args()

    # 维护者工具：从明文 JSON 生成加密 .enc 文件，不进入采集流程
    if args.encode_profile:
        in_path = Path(args.encode_profile)
        if not in_path.exists():
            print(f"❌ 输入文件不存在: {in_path}", file=sys.stderr)
            sys.exit(2)
        plain = in_path.read_bytes()
        # 校验是合法 JSON
        try:
            json.loads(plain.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"❌ 输入文件不是合法 JSON: {e}", file=sys.stderr)
            sys.exit(2)
        out_path = in_path.with_suffix(".enc")
        out_path.write_bytes(encode_profile_bytes(plain))
        # 使用 sys.stdout.buffer 直接写 utf-8，避免 Windows GBK 控制台报错
        msg = f"[OK] 已生成加密配置: {out_path} ({out_path.stat().st_size} bytes)\n"
        try:
            sys.stdout.buffer.write(msg.encode("utf-8"))
        except AttributeError:
            print(msg.strip())
        sys.exit(0)

    profile = load_profile(args.profile)
    tool_version = profile.get("tool_version", "v1.2.0")

    print("\033[1m" + "=" * 64)
    print(f"  服务器推理能力评估工具 {tool_version}")
    print("  Server Inference Capability Inspector (profile-driven)")
    print("=" * 64 + "\033[0m\n")
    progress(f"加载配置: {len(profile.get('accelerators', []))} 种加速卡, "
             f"{len(profile.get('models_2026', []))} 个模型, "
             f"{len(profile.get('scenario_templates', []))} 个场景模板")

    registry = CommandRegistry(profile)
    progress(f"扫描可用命令... ({sum(registry.summary().values())} 个有效命令)")

    hw_col = HardwareCollector(profile, registry)
    sw_col = SoftwareCollector(profile, registry)

    progress("采集 CPU 信息...")
    cpu_info = hw_col.collect_cpu()
    progress("采集内存信息...")
    mem_info = hw_col.collect_memory()
    progress("采集存储信息...")
    sto_info = hw_col.collect_storage()
    progress("采集网络信息...")
    net_info = hw_col.collect_network()
    progress("检测算力卡...")
    accel_info = hw_col.collect_accelerators()

    hw = {
        "CPU":    cpu_info,
        "内存":   mem_info,
        "存储":   sto_info,
        "网络":   net_info,
        "算力卡": accel_info,
    }

    progress("采集操作系统信息...")
    os_info = sw_col.collect_os()
    progress("采集驱动程序...")
    drv_info = sw_col.collect_drivers(accel_info)
    progress("采集 RDMA / MPI / 集群工具链...")
    cluster_info = sw_col.collect_rdma_cluster()
    progress("采集容器 / Kubernetes 环境...")
    ctr_info = sw_col.collect_containers()
    progress("检测 ML 推理框架...")
    ml_info = sw_col.collect_ml_env()

    sw = {
        "操作系统":    os_info,
        "驱动程序":    drv_info,
        "RDMA与集群":  cluster_info,
        "容器与K8s":   ctr_info,
        "ML推理框架":  ml_info,
    }

    progress("生成评估报告...")
    hostname = os_info.get("主机名", socket.gethostname())
    reporter = ReportGenerator(hw, sw, hostname, profile)

    # 检测到未收录硬件时，落地一份贡献文件；路径回填到 reporter
    contrib_path = write_contribution_file(
        hostname, hw, sw, profile, reporter.unknown_items
    )
    if contrib_path:
        reporter.contribution_path = str(contrib_path)

    md_content   = reporter.to_markdown()
    html_content = reporter.to_html(md_content)

    ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    h_short = hostname.split(".")[0]
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    md_path   = out_dir / f"report_{h_short}_{ts_str}.md"
    html_path = out_dir / f"report_{h_short}_{ts_str}.html"
    json_path = out_dir / f"report_{h_short}_{ts_str}.json"

    md_path.write_text(md_content, encoding="utf-8")
    html_path.write_text(html_content, encoding="utf-8")

    def to_json(obj):
        if isinstance(obj, dict):
            return {k: to_json(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [to_json(v) for v in obj]
        if isinstance(obj, (int, float, bool)) or obj is None:
            return obj
        return str(obj)

    json_path.write_text(
        json.dumps({"hardware": to_json(hw), "software": to_json(sw),
                    "timestamp": ts_str,
                    "tool_version": profile.get("tool_version", "v1.2.0"),
                    "profile_version": profile.get("tool_version", "?")},
                   ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print("\n" + "\033[1m" + "=" * 64)
    print("  ✅ 评估完成！")
    print(f"  📄 Markdown : {md_path}")
    print(f"  🌐 HTML     : {html_path}")
    print(f"  🗄️  原始数据 : {json_path}")
    print("=" * 64 + "\033[0m\n")

    ana = reporter.analyze()
    if ana["问题"]:
        print("\033[31m❌ 问题:\033[0m")
        for i in ana["问题"]:
            print(f"  · {i}")
    if ana["警告"]:
        print("\033[33m⚠️  警告:\033[0m")
        for w in ana["警告"]:
            print(f"  · {w}")
    if ana["正常"]:
        print("\033[32m✅ 正常:\033[0m")
        for g in ana["正常"]:
            print(f"  · {g}")

    dep = reporter.recommend()
    print(f"\n\033[1m📊 建议部署方式: {dep['部署方式']}\033[0m")
    print(f"🔧 推荐框架: {', '.join(dep['推荐框架'][:2])}")

    if reporter.contribution_path:
        print(f"\n\033[35m🆕 检测到 {len(reporter.unknown_items)} 项未收录硬件，已生成贡献文件：\033[0m")
        print(f"   {reporter.contribution_path}")
        contrib_cfg = profile.get("contribution", {})
        if contrib_cfg.get("issue_url"):
            print(f"   提交方式：Gitee Issue {contrib_cfg['issue_url']}")
        if contrib_cfg.get("contact_email"):
            print(f"            或 邮箱 {contrib_cfg['contact_email']}")
    print()


if __name__ == "__main__":
    main()
