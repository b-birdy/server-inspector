#!/usr/bin/env bash
# sync-to-gitee.sh - 同步 GitHub 仓库到 Gitee 前自动替换域名
# 用法：./sync-to-gitee.sh

set -euo pipefail

echo "Replacing GitHub references with Gitee..."

# 1. install.sh: 默认主机改为 gitee.com
sed -i 's/REPO_HOST="${REPO_HOST:-github.com}"/REPO_HOST="${REPO_HOST:-gitee.com}"/' install.sh

# 2. README.md: GitHub 链接 → Gitee 链接
sed -i 's|https://github.com/b-birdy|https://gitee.com/wzxdcyy|g' README.md

# 3. README.md: raw.githubusercontent.com → gitee.com/.../raw/
sed -i 's|https://raw.githubusercontent.com/b-birdy/server-inspector/|https://gitee.com/wzxdcyy/server-inspector/raw/|g' README.md

echo "Done. Review changes with: git diff"
echo "Then commit and push to Gitee."