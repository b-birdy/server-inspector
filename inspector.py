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
import html as html_lib
import datetime
import socket
import argparse
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional


# ─────────────────────────────────────────────
# 平台 guard
# ─────────────────────────────────────────────
if sys.platform not in ("linux", "linux2"):
    print(f"⚠️  本工具仅支持 Linux（当前平台: {sys.platform}）", file=sys.stderr)
    print("    Windows / macOS 上无法采集 Linux 专属硬件信息，请在目标 Linux 服务器上运行。", file=sys.stderr)
    sys.exit(1)


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

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

def load_profile(path: str) -> Dict:
    """加载 profiles.json。脚本同目录优先，失败则报错。"""
    p = Path(path)
    if not p.is_absolute():
        for candidate in [Path.cwd() / path, Path(__file__).parent / path]:
            if candidate.exists():
                p = candidate
                break
    if not p.exists():
        print(f"❌ 找不到配置文件: {path}", file=sys.stderr)
        sys.exit(2)
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ profiles.json 解析失败: {e}", file=sys.stderr)
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
            _, ibv_out, _ = run("ibv_devinfo 2>/dev/null | grep -E 'hca_id|active_mtu|state|rate' | head -40")
            if ibv_out:
                info["RDMA设备详情(ibv_devinfo)"] = ibv_out

        if self.reg.has("show_gids"):
            _, gids_out, _ = run("show_gids 2>/dev/null | head -10")
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
        spec = self._match_model_spec(accel, model_name, smi_outputs)

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
            "BF16 TFLOPS":      f"{spec.get('bf16_tflops','?')} TFLOPS/卡",
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
            name = outputs.get("name", "")
            m = re.search(r"(Z100\w*|K100\w*)", name, re.IGNORECASE)
            if m:
                model_name = m.group(1).upper()

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
            cnt = len(re.findall(r"NPU\s+\d+", overview))
            if cnt:
                card_count = cnt
            # 显存可能在 memory query
            mem_out = outputs.get("mem", "") + overview
            mm = re.findall(r"(\d+)\s*/\s*(\d+)\s*MB", mem_out)
            if mm:
                single_mib = int(mm[0][1])
            # 型号关键字
            for kw in ["910C", "910B4", "910B3", "910B", "910A", "910", "310P", "310"]:
                if kw in overview:
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

    def _match_model_spec(self, accel: Dict, model_name: str,
                          outputs: Dict[str, str]) -> Dict:
        """根据 model_name 在 profile.model_specs 中找匹配。"""
        for spec in accel.get("model_specs", []):
            if spec["match"].lower() in (model_name or "").lower():
                return spec
        # 二次匹配：在所有 smi 输出中找
        joined = "\n".join(outputs.values())
        for spec in accel.get("model_specs", []):
            if spec["match"].lower() in joined.lower():
                return spec
        return accel.get("default_spec", {})


# ─────────────────────────────────────────────
# 软件采集
# ─────────────────────────────────────────────

class SoftwareCollector:
    def __init__(self, profile: Dict, registry: CommandRegistry):
        self.profile = profile
        self.reg = registry

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

        _, torch_v, _ = run(
            "python3 -c 'import torch; "
            "print(torch.__version__, \"|\", \"CUDA:\", torch.cuda.is_available(), "
            "\"|\", \"Devices:\", torch.cuda.device_count())' 2>/dev/null"
        )
        info["PyTorch"] = (torch_v or "未安装").replace("|", "·")

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
        self.hostname = hostname.split(".")[0]
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
            goods.append("RDMA 工具完整 (perftest/diag 套件可用)")
        rdma_link = self.hw.get("网络", {}).get("RDMA链路状态(ibstatus)", "")
        if "ACTIVE" in rdma_link:
            active_cnt = rdma_link.count("ACTIVE")
            rate_m = re.search(r"(\d+)\s*Gb/sec", rdma_link)
            rate = rate_m.group(1) if rate_m else "?"
            goods.append(f"RDMA 链路 {active_cnt} 路 Active，速率 {rate} Gb/s")

        if self.sw.get("RDMA与集群", {}).get("MPI 实现"):
            goods.append("MPI 已安装，支持多机分布式推理/训练")

        ctr = self.sw.get("容器与K8s", {})
        if ctr.get("Docker"):
            goods.append("Docker 已安装")
        gpu_ctk = ctr.get("GPU 容器工具", {})
        if gpu_ctk:
            tools = ", ".join(gpu_ctk.keys())
            goods.append(f"GPU 容器工具链: {tools}")
        else:
            warnings.append("未检测到 GPU 容器工具，容器内调用加速卡会受限")
        if ctr.get("Kubernetes 工具链"):
            goods.append("Kubernetes 工具链已安装")

        ml = self.sw.get("ML推理框架", {})
        if ml.get("vLLM", "未安装") != "未安装":
            goods.append(f"vLLM 已安装 v{ml['vLLM']}")
        else:
            warnings.append("vLLM 未安装")

        # 特定加速卡建议
        if self.primary_card:
            pid = self.primary_card.get("_accel_id", "")
            if pid == "kunlun" and not ml.get("PaddleNLP"):
                warnings.append("昆仑芯首选推理框架 PaddleNLP 未安装")
            if pid == "ascend" and not ml.get("MindSpore"):
                warnings.append("昇腾首选框架 MindSpore 未安装")
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

    def recommend(self) -> Dict:
        cnt = self.accel_count
        has_rdma = self._has_rdma_capability()

        if cnt == 0:
            mode, tp = "CPU 推理（性能有限）", 1
            frameworks = ["llama.cpp (CPU)", "Transformers + bitsandbytes"]
        elif cnt == 1:
            mode, tp = "单卡部署", 1
        elif cnt <= 8:
            mode, tp = f"单机多卡（TP={cnt}）", cnt
        else:
            # 只有在有RDMA能力时才建议多机方案
            if has_rdma:
                mode, tp = f"多机多卡（总 TP≥{cnt}）", cnt
            else:
                mode, tp = f"单机多卡（TP={min(cnt, 8)}）", min(cnt, 8)

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
            tvram = self.total_vram_gb
            if tvram == 0:
                r["状态"] = "❌ 无算力卡"
                r["建议精度"] = "—"
            elif tvram >= m["fp16_gb"]:
                r["状态"] = "✅ 总显存充足，FP16/BF16 可直接部署"
                r["建议精度"] = "BF16"
            elif tvram >= m["int4_gb"]:
                r["状态"] = "⚠️ 仅满足量化部署"
                r["建议精度"] = "INT4/INT8 (AWQ/GPTQ)"
            else:
                r["状态"] = f"❌ 显存不足（需 ≥{m['int4_gb']}GB，现 {tvram:.0f}GB）"
                r["建议精度"] = "—"

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
        """通用性能预估：所有加速卡共享场景模板。"""
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

            # 筛选适用模型
            match_rule = tpl.get("match_models_by_fp16", {})
            preferred_model_name = tpl.get("preferred_model", "")
            applicable: List[Dict] = []
            preferred_found = False

            for model in models:
                fp16 = model["fp16_gb"]
                int4 = model["int4_gb"]
                ok = False
                precision = "BF16"
                vram_used = f"~{fp16} GB"
                if "max_gb_per_card_ratio" in match_rule:
                    cap = single_gb * match_rule["max_gb_per_card_ratio"]
                    if fp16 <= cap:
                        ok, precision, vram_used = True, "BF16", f"~{fp16} GB"
                    elif int4 <= cap:
                        ok, precision, vram_used = True, "INT4/AWQ", f"~{int4} GB"
                if "max_gb_total_ratio" in match_rule:
                    cap = total_gb * match_rule["max_gb_total_ratio"]
                    if fp16 <= cap:
                        ok, precision, vram_used = True, "BF16", f"~{fp16} GB（权重）+ KV"
                    elif int4 * 1.5 <= cap:
                        ok, precision, vram_used = True, "INT4/AWQ", f"~{int4} GB（权重）+ KV"
                if not ok:
                    continue
                # 性能区间（粗略估算）
                tps_low, tps_high = self._estimate_tps(model, spec, actual_cards, precision)
                tts_low, tts_high = self._estimate_ttft(model, spec, actual_cards)
                conc_low, conc_high = self._estimate_concurrency(model, total_gb, fp16, int4, precision)
                throughput_low = tps_low * conc_low // 2
                throughput_high = tps_high * conc_high // 2

                entry = {
                    "模型":      model["name"],
                    "精度":      precision,
                    "显存占用":  vram_used,
                    "TTFT":      f"{tts_low}-{tts_high} ms",
                    "单流 TPS":  f"{tps_low}-{tps_high} tok/s",
                    "并发(估)":  f"{conc_low}-{conc_high}",
                    "总吞吐":    f"{throughput_low}-{throughput_high} tok/s",
                    "说明":      model.get("notes", ""),
                    "_preferred": model["name"] == preferred_model_name,
                }
                applicable.append(entry)
                if entry["_preferred"]:
                    preferred_found = True

            if not applicable:
                continue

            # 优先排列preferred_model
            if preferred_found:
                applicable.sort(key=lambda x: not x["_preferred"])

            # 移除_preferred字段后返回
            for item in applicable:
                del item["_preferred"]

            applicable = applicable[:3]

            entry = {
                "场景":     tpl["name"],
                "硬件":     f"{actual_cards} × {display} {card_name} = "
                            f"{total_gb:.0f} GB HBM / {tflops_str} BF16",
                "适用模型": applicable,
                "推理框架": " / ".join(frameworks[:2]),
                "建议用途": tpl.get("use_case", ""),
            }
            if tpl.get("id") == "multi_node_16":
                entry["通信"] = f"节点内 {interconnect} + 节点间 RoCE/NDR 400Gb"
            if tpl.get("prerequisites"):
                entry["前置条件"] = tpl["prerequisites"]
            scns.append(entry)

        return scns

    def _estimate_tps(self, model: Dict, spec: Dict, ncards: int, precision: str) -> Tuple[int, int]:
        """粗略推断单流 TPS 区间。基于卡的 BF16 TFLOPS、激活参数量、并行度。"""
        tflops_total = spec.get("bf16_tflops", 100) * ncards
        if precision.startswith("INT"):
            tflops_total *= 1.6
        active_str = model.get("active", "10B").replace("~", "").upper()
        m = re.search(r"([\d.]+)", active_str)
        active_b = float(m.group(1)) if m else 10
        # 启发式: TPS ≈ tflops / (2 * active_params * batch_factor)
        base = tflops_total / max(active_b, 1) / 2
        # MoE 模型给加成
        if "MoE" in model.get("params", ""):
            base *= 1.4
        low = max(int(base * 0.4), 20)
        high = max(int(base * 0.8), low + 10)
        return low, high

    def _estimate_ttft(self, model: Dict, spec: Dict, ncards: int) -> Tuple[int, int]:
        """首 token 延迟启发式估算，单位 ms。"""
        active_str = model.get("active", "10B").replace("~", "").upper()
        m = re.search(r"([\d.]+)", active_str)
        active_b = float(m.group(1)) if m else 10
        tflops_total = spec.get("bf16_tflops", 100) * ncards
        # 假设 prefill 512 token: 计算量 = 2 * params * seqlen
        prefill_tflops = 2 * active_b * 512 / 1000
        ttft_ms = (prefill_tflops / max(tflops_total, 1)) * 1000 * 50
        low = max(50, int(ttft_ms * 0.7))
        high = max(low + 50, int(ttft_ms * 1.5))
        return low, high

    def _estimate_concurrency(self, model: Dict, total_gb: float,
                              fp16_gb: float, int4_gb: float,
                              precision: str) -> Tuple[int, int]:
        weight_gb = int4_gb if precision.startswith("INT") else fp16_gb
        kv_budget = max(0.1, total_gb - weight_gb)
        # 每个并发约 0.5GB KV（保守）
        max_conc = int(kv_budget / 0.5)
        low = max(4, max_conc // 3)
        high = max(low + 4, max_conc)
        return low, high

    def _parse_rdma_ibstatus(self, ibstatus_text: str) -> List[Dict]:
        """解析ibstatus输出，提取RDMA链路状态信息。"""
        if not ibstatus_text or ibstatus_text == "未检测":
            return []
        results = []
        lines = ibstatus_text.split("\n")
        for line in lines:
            line = line.strip()
            if not line or "---" in line or "Infiniband" in line:
                continue
            # 尝试解析 "mlx5_0: HCA is UP" 或 "mlx5_0/1:     DOWN 200Gb/sec"
            if "HCA is UP" in line:
                parts = line.split(":")
                if parts:
                    device = parts[0].strip()
                    results.append({"设备": device, "状态": "UP"})
            elif "DOWN" in line or "ACTIVE" in line or "FAILED" in line:
                parts = line.split()
                if len(parts) >= 2:
                    device = parts[0].strip().rstrip(":")
                    status = "ACTIVE" if "ACTIVE" in line else ("DOWN" if "DOWN" in line else "FAILED")
                    rate = "未知"
                    for p in parts:
                        if "Gb/sec" in p or "Mb/sec" in p:
                            rate = p
                    results.append({"设备": device, "状态": status, "速率": rate})
        return results if results else [{"设备": "未检测", "状态": "—", "速率": "—"}]

    def _parse_rdma_devinfo(self, devinfo_text: str) -> List[Dict]:
        """解析ibv_devinfo输出，提取RDMA设备信息。"""
        if not devinfo_text or devinfo_text == "未检测":
            return []
        results = []
        current_device = {}
        for line in devinfo_text.split("\n"):
            line = line.strip()
            if not line:
                if current_device and "HCA名称" in current_device:
                    results.append(current_device)
                    current_device = {}
                continue
            if "hca_id:" in line.lower():
                if current_device and "HCA名称" in current_device:
                    results.append(current_device)
                current_device = {"HCA名称": line.split(":")[-1].strip()}
            elif "active_mtu:" in line.lower():
                current_device["MTU"] = line.split(":")[-1].strip()
            elif "state:" in line.lower() and "ACTIVE" in line:
                current_device["状态"] = "ACTIVE"
            elif "rate:" in line.lower() or "active_speed" in line.lower():
                current_device["速率"] = line.split(":")[-1].strip()
        if current_device and "HCA名称" in current_device:
            results.append(current_device)
        return results if results else [{"HCA名称": "未检测", "状态": "—"}]

    def _parse_rdma_gids(self, gids_text: str) -> List[Dict]:
        """解析RoCE GID表。"""
        if not gids_text or gids_text == "未检测":
            return []
        results = []
        lines = gids_text.split("\n")
        for line in lines:
            line = line.strip()
            if not line or "GID" in line or "---" in line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                gid_idx = parts[0].strip(":")
                gid_addr = " ".join(parts[1:3]) if len(parts) >= 3 else parts[1]
                gid_type = "IPv4" if "." in gid_addr else ("IPv6" if ":" in gid_addr else "IB")
                results.append({"GID索引": gid_idx, "地址": gid_addr, "类型": gid_type})
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
                a("| 设备 | 状态 | 速率 |")
                a("|:----|:----|:----|")
                for item in rdma_status:
                    a(f"| {item.get('设备','—')} | {item.get('状态','—')} | {item.get('速率','—')} |")
                a("")

        # RDMA设备详情
        if net.get("RDMA设备详情(ibv_devinfo)"):
            a("**RDMA 设备详情 (ibv_devinfo)：**")
            a("")
            rdma_devinfo = self._parse_rdma_devinfo(net["RDMA设备详情(ibv_devinfo)"])
            if rdma_devinfo:
                a("| HCA 名称 | MTU | 状态 | 速率 |")
                a("|:----|:----|:----|:----|")
                for item in rdma_devinfo:
                    a(f"| {item.get('HCA名称','—')} | {item.get('MTU','—')} | {item.get('状态','—')} | {item.get('速率','—')} |")
                a("")

        # RoCE GID表
        if net.get("RoCE GID表"):
            a("**RoCE GID 表：**")
            a("")
            rdma_gids = self._parse_rdma_gids(net["RoCE GID表"])
            if rdma_gids:
                a("| GID 索引 | 地址 | 类型 |")
                a("|:----|:----|:----|")
                for item in rdma_gids:
                    a(f"| {item.get('GID索引','—')} | {item.get('地址','—')} | {item.get('类型','—')} |")
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
            spec = card["_spec"]
            for k in ["卡型号", "卡数", "单卡显存", "总显存", "BF16 TFLOPS", "PCIe", "互联", "TDP"]:
                if k in card:
                    bold = k in ("卡型号", "卡数", "单卡显存", "总显存")
                    val = card[k]
                    a(f"| {k} | **{val}** |" if bold else f"| {k} | {val} |")
            a(f"| HBM 带宽 | {spec.get('hbm_bw_tbps','?')} TB/s |")
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
            a(f"### {self.primary_card['_display_name']} 硬件规格参考")
            a("")
            a("| 参数 | 值 |")
            a("|:----|:----|")
            a(f"| 型号 | {self.primary_card.get('卡型号','?')} |")
            a(f"| BF16 算力 | {spec.get('bf16_tflops','?')} TFLOPS |")
            a(f"| INT8 算力 | {spec.get('int8_tops','?')} TOPS |")
            a(f"| HBM 容量 | {spec.get('hbm_gb','?')} GB |")
            a(f"| HBM 带宽 | {spec.get('hbm_bw_tbps','?')} TB/s |")
            a(f"| 功耗 (TDP) | {spec.get('tdp_w','?')} W |")
            a(f"| PCIe / 形态 | {spec.get('pcie','?')} |")
            a(f"| 互联 | {spec.get('interconnect','?')} |")
            a("")

        a("## 🤖 五、模型兼容性评估")
        a("")
        a("> 基于当前硬件配置对 **2026 年主流开源大语言模型** 的部署可行性评估")
        a("")
        a("| 模型 | 厂商 | 参数 | 激活 | 上下文 | FP16 需求 | INT4 需求 | 状态 | 建议精度 | 框架适配 |")
        a("|:----|:----|:----|:----|:----:|----:|----:|:----|:----:|:----|")
        for m in mods:
            a(
                f"| **{m['name']}** | {m['vendor']} | {m['params']} | {m['active']} "
                f"| {m['context']} | {m['fp16_gb']} GB | {m['int4_gb']} GB "
                f"| {m['状态']} | {m['建议精度']} | {m['框架适配']} |"
            )
        a("")

        a("## 📊 六、性能预估")
        a("")
        a("> 以下数据基于卡的标称算力 + 模型激活参数量估算，实际值受量化方式、上下文长度、并发模式影响")
        a("")
        if not scns:
            a("*未匹配到适用场景（可能是显存不足或卡型未识别）*")
            a("")
        else:
            for idx, s in enumerate(scns, 1):
                a(f"### 6.{idx} {s['场景']}")
                a("")
                a(f"**硬件配置：** {s['硬件']}")
                a("")
                if s.get("通信"):
                    a(f"**通信架构：** {s['通信']}")
                    a("")
                if s.get("前置条件"):
                    a("**前置条件：**")
                    for p in s["前置条件"]:
                        a(f"- {p}")
                    a("")
                a("**推理性能预估：**")
                a("")
                a("| 模型 | 精度 | 显存占用 | TTFT | 单流 TPS | 并发(估) | 总吞吐 | 说明 |")
                a("|:----|:----:|:----|:----:|:----:|:----:|:----:|:----|")
                for mp in s["适用模型"]:
                    a(
                        f"| **{mp['模型']}** | {mp.get('精度','—')} | "
                        f"{mp.get('显存占用','—')} | {mp.get('TTFT','—')} | "
                        f"{mp.get('单流 TPS','—')} | {mp.get('并发(估)','—')} | "
                        f"{mp.get('总吞吐','—')} | {mp.get('说明','—')} |"
                    )
                a("")
                if s.get("推理框架"):
                    a(f"**推荐框架：** {s['推理框架']}")
                    a("")
                if s.get("建议用途"):
                    a(f"**建议用途：** {s['建议用途']}")
                    a("")

        a("### 性能指标说明")
        a("")
        a("| 指标 | 全称 | 说明 |")
        a("|:----|:----|:----|")
        a("| TTFT | Time To First Token | 首 Token 延迟（用户感知的关键指标） |")
        a("| TPS | Tokens Per Second | 单流生成速度 |")
        a("| 并发 | Concurrent Sequences | 服务能稳定支撑的并发数 |")
        a("| 总吞吐 | Total Throughput | 多并发下系统总 token/s |")
        a("| MFU | Model FLOP Utilization | 算力利用率，越高越好 |")
        a("")

        a("---")
        a("")
        a("> **免责声明**：以上性能评估数据基于理论计算和经验估算，实际推理性能受量化方式、上下文长度、并发模式、框架优化等因素影响，仅供参考。请在实际部署前进行充分的基准测试。")
        a("")
        a("---")
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
table{border-collapse:separate;border-spacing:0;width:100%;margin:14px 0 22px;
      font-size:.92em;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;
      box-shadow:0 1px 3px rgba(0,0,0,.04)}
thead tr{background:linear-gradient(90deg,#3b82f6,#6366f1);color:#fff}
th{padding:11px 16px;text-align:left;font-weight:600;letter-spacing:.3px;
   white-space:nowrap;border:none}
tbody tr{transition:background .15s}
tbody tr:nth-child(even){background:#f9fafb}
tbody tr:hover{background:#eff6ff}
td{padding:9px 16px;border-bottom:1px solid #f1f5f9;vertical-align:top}
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
                out.append("</tbody></table>")
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
                    out.append('<table>')
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
    <span class="meta-value">Server Inspector v1.2.0</span>
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
    parser = argparse.ArgumentParser(description="服务器推理能力评估工具 v1.2.0")
    parser.add_argument("--output-dir", default="./reports",
                        help="报告输出目录 (默认: ./reports)")
    parser.add_argument("--profile", default="profiles.json",
                        help="硬件配置文件路径 (默认: profiles.json，脚本同目录)")
    args = parser.parse_args()

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

    md_content   = reporter.to_markdown()
    html_content = reporter.to_html(md_content)

    ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    h_short = hostname.split(".")[0]
    out_dir = Path(args.output_dir)
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
    print()


if __name__ == "__main__":
    main()
