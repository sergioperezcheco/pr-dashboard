#!/usr/bin/env bash
# update_dashboard.sh — 被 cron job 调用
# 1. 运行数据收集脚本更新 docs/data.json
# 2. git commit + push 到 GitHub（触发 Pages 自动重新部署）
# 3. 输出需要 action 的 PR 清单（供 agent 后续处理）
set -euo pipefail

DASHBOARD_DIR="/Volumes/666/oss-contrib/pr-dashboard"
cd "$DASHBOARD_DIR"

echo "=== 更新 PR Dashboard 数据 ==="
python3 scripts/collect_pr_data.py 2>&1

echo ""
echo "=== Git commit + push ==="
# 只有 data.json 变了才 commit
if git diff --quiet docs/data.json 2>/dev/null; then
    echo "data.json 无变化，跳过 commit"
else
    # 配置 git（如果还没配置）
    git config user.name "sergioperezcheco" 2>/dev/null || true
    git config user.email "checo520@outlook.com" 2>/dev/null || true
    
    git add docs/data.json
    git commit -m "data: update PR dashboard $(date -u +%Y-%m-%dT%H:%M)"
    git push origin main 2>&1 | tail -3
    echo "✓ 已推送到 GitHub，Pages 会自动重新部署"
fi

echo ""
echo "=== 需要处理的 PR 清单 ==="
# 从 data.json 提取 needs_action 的 PR
python3 -c "
import json
with open('docs/data.json') as f:
    data = json.load(f)

action_prs = [p for p in data['prs'] if p.get('needs_action') and p['state'] == 'open']
if not action_prs:
    print('（无）')
else:
    order = {'high': 0, 'medium': 1, 'low': 2}
    action_prs.sort(key=lambda p: order.get(p.get('action_priority', 'low'), 3))
    for p in action_prs:
        print(f\"[{p['action_priority'].upper()}] {p['repo']} #{p['number']}: {p['title'][:60]}\")
        for r in p.get('action_reasons', []):
            print(f'  {r}')
        print(f'  URL: {p[\"url\"]}')
        print()
"
