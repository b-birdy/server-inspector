#!/usr/bin/env bash
set -euo pipefail
APP=server-inspector
REPO="${REPO:-b-birdy/server-inspector}"
REPO_HOST="${REPO_HOST:-github.com}"

MUTED='\033[0;2m'
RED='\033[0;31m'
ORANGE='\033[38;5;214m'
GREEN='\033[0;32m'
NC='\033[0m'

usage() {
    cat <<EOF
Server Inspector Installer

Usage: install.sh [options]

Options:
    -h, --help              Display this help message
    -v, --version <version> Install a specific version (tag or branch)
    -d, --dir <path>        Install to specific directory (default: ~/.local/share/server-inspector)
        --no-modify-path    Don't modify shell config files
        --uninstall         Remove server-inspector

Environment:
    REPO_HOST               Download host (default: github.com, or gitee.com)

Examples:
    curl -fsSL https://your-domain.com/install | bash
    curl -fsSL https://your-domain.com/install | bash -s -- --version v1.2.0
    REPO_HOST=gitee.com ./install.sh
EOF
}

requested_version=${VERSION:-}
install_dir=""
no_modify_path=false
uninstall=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        -v|--version)
            if [[ -n "${2:-}" ]]; then
                requested_version="$2"
                shift 2
            else
                echo -e "${RED}Error: --version requires a version argument${NC}"
                exit 1
            fi
            ;;
        -d|--dir)
            if [[ -n "${2:-}" ]]; then
                install_dir="$2"
                shift 2
            else
                echo -e "${RED}Error: --dir requires a path argument${NC}"
                exit 1
            fi
            ;;
        --no-modify-path)
            no_modify_path=true
            shift
            ;;
        --uninstall)
            uninstall=true
            shift
            ;;
        *)
            echo -e "${ORANGE}Warning: Unknown option '$1'${NC}" >&2
            shift
            ;;
    esac
done

# Default install directory
if [[ -z "$install_dir" ]]; then
    install_dir="$HOME/.local/share/server-inspector"
fi

BIN_DIR="$install_dir"
INSTALL_DIR="$install_dir/repo"
WRAPPER="$BIN_DIR/server-inspector"

# ─── Uninstall ───
if [[ "$uninstall" == true ]]; then
    uninstalled=false

    # Remove the ~/.local/bin command symlink (if present and points to us)
    user_bin_link="$HOME/.local/bin/server-inspector"
    if [[ -L "$user_bin_link" ]]; then
        link_target=$(readlink "$user_bin_link" 2>/dev/null || true)
        case "$link_target" in
            *server-inspector*)
                rm -f "$user_bin_link"
                echo -e "${GREEN}Removed command symlink: $user_bin_link${NC}"
                uninstalled=true
                ;;
        esac
    fi

    if [[ -d "$install_dir" ]]; then
        rm -rf "$install_dir"
        echo -e "${GREEN}Uninstalled $APP from $install_dir${NC}"
        uninstalled=true
    fi

    # Remove PATH entries from shell config files
    XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
    current_shell=$(basename "$SHELL")
    case $current_shell in
        fish) shell_configs="$HOME/.config/fish/config.fish" ;;
        zsh)  shell_configs="${ZDOTDIR:-$HOME}/.zshrc ${ZDOTDIR:-$HOME}/.zshenv" ;;
        *)    shell_configs="$HOME/.bashrc $HOME/.bash_profile $HOME/.profile" ;;
    esac

    # 删除我们添加的两行（# server-inspector 注释 + 紧随的 PATH 行），
    # 避免误删用户自己加的其它 PATH 配置。
    for cfg in $shell_configs; do
        if [[ -f "$cfg" ]]; then
            sed -i '/^# server-inspector$/,+1d' "$cfg" 2>/dev/null || true
        fi
    done

    if [[ "$uninstalled" == true ]]; then
        echo -e "${GREEN}PATH entries removed from shell config.${NC}"
    else
        echo -e "${ORANGE}$APP is not installed at $install_dir${NC}"
    fi
    exit 0
fi

# ─── Pre-flight checks ───
print_message() {
    local level=$1
    local message=$2
    local color=""
    case $level in
        info) color="${NC}" ;;
        warning) color="${ORANGE}" ;;
        error) color="${RED}" ;;
        success) color="${GREEN}" ;;
    esac
    echo -e "${color}${message}${NC}"
}

# Check OS (Linux only, matching inspector.py)
raw_os=$(uname -s)
os=$(echo "$raw_os" | tr '[:upper:]' '[:lower:]')
case "$raw_os" in
    Linux*) os="linux" ;;
    *)
        print_message error "Unsupported OS: $raw_os"
        print_message info "Server Inspector only supports Linux."
        exit 1
        ;;
esac

# Check Python >= 3.6
if ! command -v python3 >/dev/null 2>&1; then
    print_message error "python3 is required but not installed."
    exit 1
fi

py_version=$(python3 --version 2>/dev/null | awk '{print $2}')
py_major=$(echo "$py_version" | cut -d. -f1)
py_minor=$(echo "$py_version" | cut -d. -f2)

if [[ "$py_major" -lt 3 || ("$py_major" -eq 3 && "$py_minor" -lt 6) ]]; then
    print_message error "Python 3.6+ required, found $py_version"
    exit 1
fi

# Check dependencies: curl or wget
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    print_message error "curl or wget is required but neither is installed."
    exit 1
fi

# ─── Resolve version ───
if [[ -z "$requested_version" ]]; then
    version_ref="master"
    version_display="latest (master)"
else
    version_ref="$requested_version"
    version_display="$requested_version"
fi

# ─── Install ───
mkdir -p "$BIN_DIR"

# Construct raw file download URL
get_raw_url() {
    local host=$1 repo=$2 ref=$3 file=$4
    if [[ "$host" == "github.com" ]]; then
        echo "https://raw.githubusercontent.com/${repo}/${ref}/${file}"
    else
        echo "https://${host}/${repo}/raw/${ref}/${file}"
    fi
}

# Download helper: try all available tools
download_file() {
    local url=$1 out=$2
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$out" "$url" 2>/dev/null
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$out" "$url" 2>/dev/null
    else
        return 1
    fi
}

# Clean slate
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

print_message info "${MUTED}Downloading $REPO ($version_display)...${NC}"

# Download runtime files
for file in inspector.py profiles.enc; do
    url=$(get_raw_url "$REPO_HOST" "$REPO" "$version_ref" "$file")
    if ! download_file "$url" "$INSTALL_DIR/$file"; then
        print_message error "Failed to download $file from $REPO_HOST"
        exit 1
    fi
    print_message success "Downloaded $file"
done

# Verify inspector.py exists
if [[ ! -f "$INSTALL_DIR/inspector.py" ]]; then
    print_message error "inspector.py not found after download. Installation failed."
    exit 1
fi

# Verify profiles.enc exists
if [[ ! -f "$INSTALL_DIR/profiles.enc" ]]; then
    print_message warning "profiles.enc not found. The tool may not work correctly."
fi

# Create wrapper script
# 注意：用 readlink -f 解析 symlink，让 wrapper 通过 ~/.local/bin 的 symlink
# 调用时仍能正确定位 repo 目录。
cat > "$WRAPPER" <<'WRAPPER_EOF'
#!/usr/bin/env bash
# Server Inspector launcher
# Auto-generated by install.sh

set -euo pipefail

# Resolve the real script location, following symlinks (e.g., ~/.local/bin link)
if command -v readlink >/dev/null 2>&1 && readlink -f / >/dev/null 2>&1; then
    REAL_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
else
    # Fallback for systems without GNU readlink -f
    REAL_PATH="${BASH_SOURCE[0]}"
    while [[ -L "$REAL_PATH" ]]; do
        link_target="$(readlink "$REAL_PATH")"
        if [[ "$link_target" = /* ]]; then
            REAL_PATH="$link_target"
        else
            REAL_PATH="$(cd "$(dirname "$REAL_PATH")" && cd "$(dirname "$link_target")" && pwd)/$(basename "$link_target")"
        fi
    done
fi

SCRIPT_DIR="$(cd "$(dirname "$REAL_PATH")" && pwd)"
REPO_DIR="$SCRIPT_DIR/repo"
PYTHON="${SERVER_INSPECTOR_PYTHON:-python3}"

if [[ ! -f "$REPO_DIR/inspector.py" ]]; then
    echo "Error: inspector.py not found at $REPO_DIR" >&2
    exit 1
fi

exec "$PYTHON" "$REPO_DIR/inspector.py" "$@"
WRAPPER_EOF

chmod +x "$WRAPPER"

# ─── Symlink to ~/.local/bin (XDG standard user bin) ───
# 优先把命令入口放到 ~/.local/bin，这是大多数现代 Linux 发行版
# (Ubuntu 20.04+/Debian 10+/Fedora/RHEL8+/Rocky/Alma) 默认就在 PATH 里的目录。
# 这样很多用户安装完无需 source 任何文件即可直接使用 server-inspector 命令。
USER_BIN="$HOME/.local/bin"
SYMLINK="$USER_BIN/server-inspector"
symlink_ok=false

if mkdir -p "$USER_BIN" 2>/dev/null && [[ -w "$USER_BIN" ]]; then
    if ln -sfn "$WRAPPER" "$SYMLINK" 2>/dev/null; then
        symlink_ok=true
        print_message success "Linked command: $SYMLINK"
    fi
fi

# ─── PATH setup ───
add_to_path() {
    local config_file=$1
    local command=$2
    if grep -Fxq "$command" "$config_file" 2>/dev/null; then
        print_message info "PATH already configured in $config_file, skipping."
    elif [[ -w $config_file ]]; then
        echo -e "\n# server-inspector" >> "$config_file"
        echo "$command" >> "$config_file"
        print_message success "Added to PATH in $config_file"
    else
        print_message warning "Cannot write to $config_file. Add manually:"
        print_message info "  $command"
    fi
}

XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
current_shell=$(basename "$SHELL")

case $current_shell in
    fish)
        config_files="$HOME/.config/fish/config.fish"
        ;;
    zsh)
        config_files="${ZDOTDIR:-$HOME}/.zshrc ${ZDOTDIR:-$HOME}/.zshenv $XDG_CONFIG_HOME/zsh/.zshrc $XDG_CONFIG_HOME/zsh/.zshenv"
        ;;
    bash)
        config_files="$HOME/.bashrc $HOME/.bash_profile $HOME/.profile $XDG_CONFIG_HOME/bash/.bashrc $XDG_CONFIG_HOME/bash/.bash_profile"
        ;;
    ash)
        config_files="$HOME/.ashrc $HOME/.profile /etc/profile"
        ;;
    sh)
        config_files="$HOME/.ashrc $HOME/.profile /etc/profile"
        ;;
    *)
        config_files="$HOME/.bashrc $HOME/.bash_profile $XDG_CONFIG_HOME/bash/.bashrc $XDG_CONFIG_HOME/bash/.bash_profile"
        ;;
esac

# 决定本次安装需要让哪个目录出现在 PATH：
#   - symlink 创建成功 → 使用 $USER_BIN (~/.local/bin)，多数发行版默认已在 PATH
#   - symlink 失败（无权限/异常）→ fallback 到 $BIN_DIR（旧逻辑）
if [[ "$symlink_ok" == true ]]; then
    PATH_TARGET="$USER_BIN"
else
    PATH_TARGET="$BIN_DIR"
fi

# 判断当前 shell 启动时 PATH 已经包含目标目录 → 不需要改 rc 文件
case ":${PATH:-}:" in
    *":$PATH_TARGET:"*) path_already_present=true ;;
    *) path_already_present=false ;;
esac

config_file=""
if [[ "$no_modify_path" != true && "$path_already_present" != true ]]; then
    for file in $config_files; do
        if [[ -f $file ]]; then
            config_file=$file
            break
        fi
    done

    if [[ -z $config_file ]]; then
        print_message warning "No shell config found. Add to PATH manually:"
        print_message info "  export PATH=\"$PATH_TARGET:\$PATH\""
    else
        case $current_shell in
            fish)
                add_to_path "$config_file" "fish_add_path $PATH_TARGET"
                ;;
            *)
                add_to_path "$config_file" "export PATH=\"$PATH_TARGET:\$PATH\""
                ;;
        esac
    fi
elif [[ "$path_already_present" == true ]]; then
    print_message info "${MUTED}$PATH_TARGET already in PATH${NC}"
fi

# GitHub Actions support
if [[ -n "${GITHUB_ACTIONS:-}" ]] && [[ "${GITHUB_ACTIONS}" == "true" ]]; then
    echo "$PATH_TARGET" >> "$GITHUB_PATH"
    print_message info "Added to \$GITHUB_PATH"
fi

# ─── Done ───
echo ""
print_message success "Server Inspector installed successfully!"
echo ""
echo -e "${MUTED}Version:${NC}    $version_display"
echo -e "${MUTED}Location:${NC}   $INSTALL_DIR"
echo -e "${MUTED}Command:${NC}    server-inspector"
echo ""
echo -e "${MUTED}Quick start:${NC}"
echo "  server-inspector                    ${MUTED}# Run with defaults${NC}"
echo "  server-inspector --help             ${MUTED}# Show help${NC}"
echo "  server-inspector --output-dir ./reports ${MUTED}# Specify output dir${NC}"
echo ""

# PATH activation guidance
# install.sh 在子进程中运行，无法修改父 shell 的 PATH，
# 所以这里只能根据 PATH 状态输出对应引导。
if [[ "$path_already_present" == true ]]; then
    # 目标目录在 PATH 中（通常是 ~/.local/bin），命令立即可用
    echo -e "${GREEN}✅ server-inspector 命令已可在当前 shell 直接使用${NC}"
    echo -e "   验证: ${GREEN}server-inspector --help${NC}"
    echo ""
elif [[ "$no_modify_path" != true && -n "${config_file:-}" ]]; then
    echo -e "${ORANGE}⚠ 当前 shell 还无法识别 server-inspector 命令${NC}"
    echo -e "${MUTED}(PATH 配置已写入 $config_file，但需要在你自己的 shell 中重新加载)${NC}"
    echo ""
    echo -e "请任选其一让命令生效："
    echo -e "  ${GREEN}A.${NC} 在当前 shell 执行: ${GREEN}source $config_file${NC}"
    echo -e "  ${GREEN}B.${NC} 或重启当前 shell:   ${GREEN}exec \$SHELL -l${NC}"
    echo -e "  ${GREEN}C.${NC} 或打开一个新终端"
    echo ""
    echo -e "${MUTED}也可直接用绝对路径立即验证安装结果：${NC}"
    echo -e "  ${GREEN}$WRAPPER --help${NC}"
    echo ""
fi

echo -e "${MUTED}Project:${NC} https://$REPO_HOST/$REPO"