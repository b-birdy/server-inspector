#!/usr/bin/env bash
set -euo pipefail
APP=server-inspector
REPO="b-birdy/server-inspector"

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

Examples:
    curl -fsSL https://your-domain.com/install | bash
    curl -fsSL https://your-domain.com/install | bash -s -- --version v1.2.0
    ./install.sh --dir /opt/server-inspector
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
    if [[ -d "$install_dir" ]]; then
        rm -rf "$install_dir"
        echo -e "${GREEN}Uninstalled $APP from $install_dir${NC}"
        echo -e "${ORANGE}Note: You may need to manually remove PATH entries from your shell config.${NC}"
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

# Check git
if ! command -v git >/dev/null 2>&1; then
    print_message error "git is required but not installed."
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

# Clone or update
if [[ -d "$INSTALL_DIR/.git" ]]; then
    print_message info "${MUTED}Updating existing installation...${NC}"
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$version_ref" 2>/dev/null || true
    git -C "$INSTALL_DIR" reset --hard "origin/$version_ref" 2>/dev/null || \
    git -C "$INSTALL_DIR" checkout "$version_ref" 2>/dev/null || true
else
    rm -rf "$INSTALL_DIR"
    print_message info "${MUTED}Cloning $REPO ($version_display)...${NC}"
    # Try SSH first (port 22, faster), fall back to HTTPS (port 443)
    if ! git clone --depth 1 --branch "$version_ref" "git@github.com:$REPO.git" "$INSTALL_DIR" 2>/dev/null; then
        if ! git clone --depth 1 --branch "$version_ref" "https://github.com/$REPO.git" "$INSTALL_DIR" 2>/dev/null; then
            print_message error "Failed to clone $REPO. Check network connectivity."
            exit 1
        fi
    fi
fi

# Verify inspector.py exists
if [[ ! -f "$INSTALL_DIR/inspector.py" ]]; then
    print_message error "inspector.py not found after clone. Installation failed."
    exit 1
fi

# Verify profiles.enc exists
if [[ ! -f "$INSTALL_DIR/profiles.enc" ]]; then
    print_message warning "profiles.enc not found. The tool may not work correctly."
fi

# Create wrapper script
cat > "$WRAPPER" <<'WRAPPER_EOF'
#!/usr/bin/env bash
# Server Inspector launcher
# Auto-generated by install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$SCRIPT_DIR/repo"
PYTHON="${SERVER_INSPECTOR_PYTHON:-python3}"

if [[ ! -f "$REPO_DIR/inspector.py" ]]; then
    echo "Error: inspector.py not found at $REPO_DIR" >&2
    exit 1
fi

exec "$PYTHON" "$REPO_DIR/inspector.py" "$@"
WRAPPER_EOF

chmod +x "$WRAPPER"

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

if [[ "$no_modify_path" != true ]]; then
    config_file=""
    for file in $config_files; do
        if [[ -f $file ]]; then
            config_file=$file
            break
        fi
    done

    if [[ -z $config_file ]]; then
        print_message warning "No shell config found. Add to PATH manually:"
        print_message info '  export PATH="$BIN_DIR:$PATH"'
    elif [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
        case $current_shell in
            fish)
                add_to_path "$config_file" "fish_add_path '$BIN_DIR'"
                ;;
            *)
                add_to_path "$config_file" 'export PATH="$BIN_DIR:$PATH"'
                ;;
        esac
    else
        print_message info "${MUTED}$BIN_DIR already in PATH${NC}"
    fi
fi

# GitHub Actions support
if [[ -n "${GITHUB_ACTIONS:-}" ]] && [[ "${GITHUB_ACTIONS}" == "true" ]]; then
    echo "$BIN_DIR" >> "$GITHUB_PATH"
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

# If PATH was modified, remind to source or restart
if [[ "$no_modify_path" != true && -n "${config_file:-}" ]] && [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo -e "${ORANGE}Run this to use server-inspector in current session:${NC}"
    echo -e "  source $config_file"
    echo ""
fi

echo -e "${MUTED}Project:${NC} https://github.com/$REPO"
