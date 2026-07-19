#!/usr/bin/env python3
"""
PR Dashboard 数据收集脚本
从 GitHub API 拉取 sergioperezcheco 的所有 PR，输出 data.json 供前端展示。

用法:
    python3 collect_pr_data.py              # 拉取所有 PR（open + closed + merged）
    python3 collect_pr_data.py --open-only  # 只拉取 open PR（更快，适合 cron 频繁更新）

输出: data.json （放在脚本同级的 ../web/ 目录下）
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AUTHOR = "sergioperezcheco"
# Bot 用户名列表（这些用户的评论/review 不算"需要 action"）
BOT_USERS = {
    "github-actions[bot]", "vercel[bot]", "dependabot[bot]",
    "CLAassistant", "netlify[bot]", "stale[bot]", "mergify[bot]",
    "semantic-release-bot", "renovate[bot]", "codecov[bot]",
}
# Self user
SELF_USER = AUTHOR

SCRIPT_DIR = Path(__file__).parent.resolve()
WEB_DIR = SCRIPT_DIR.parent / "web"
OUTPUT_FILE = WEB_DIR / "data.json"


def run_gh(*args, timeout=60):
    """运行 gh CLI 命令，返回解析后的 JSON。"""
    cmd = ["gh"] + list(args)
    # 用 --jq 或 --json 时 gh 输出 JSON
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
            check=False
        )
        if result.returncode != 0:
            return None
        output = result.stdout.strip()
        if not output:
            return None
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return output
    except subprocess.TimeoutExpired:
        return None


def fetch_all_prs():
    """拉取所有 PR（open + closed，含 merged）。分两次查询后合并。"""
    print(f"[fetch] 拉取 {AUTHOR} 的所有 PR (open)...", file=sys.stderr)
    open_prs = run_gh(
        "search", "prs",
        f"--author={AUTHOR}",
        "--state=open",
        "--limit=200",
        "--json=repository,number,state,title,url,createdAt,closedAt,updatedAt"
    ) or []
    print(f"[fetch] 拉取 {AUTHOR} 的所有 PR (closed)...", file=sys.stderr)
    closed_prs = run_gh(
        "search", "prs",
        f"--author={AUTHOR}",
        "--state=closed",
        "--limit=200",
        "--json=repository,number,state,title,url,createdAt,closedAt,updatedAt"
    ) or []
    prs = open_prs + closed_prs
    # 去重（理论上不会重复，但保险）
    seen = set()
    unique = []
    for p in prs:
        key = (p["repository"]["nameWithOwner"], p["number"])
        if key not in seen:
            seen.add(key)
            unique.append(p)
    prs = unique
    if not prs:
        print("[error] 无法拉取 PR 列表", file=sys.stderr)
        return []
    print(f"[fetch] 获取到 {len(prs)} 个 PR (open={len(open_prs)}, closed={len(closed_prs)})",
          file=sys.stderr)
    return prs


def fetch_pr_detail(repo, number):
    """拉取单个 PR 的详细信息：mergeable_state, reviewDecision, labels, additions/deletions"""
    detail = run_gh(
        "pr", "view", str(number),
        "-R", repo,
        "--json=mergeable,mergeStateStatus,reviewDecision,isDraft,additions,deletions,changedFiles,headRefName,baseRefName,labels",
        timeout=30
    )
    if not detail:
        return {}
    return detail


def fetch_pr_comments(repo, number):
    """拉取 issue comments + reviews，返回最后几条。"""
    comments = run_gh(
        "api", f"repos/{repo}/issues/{number}/comments",
        "--jq=[.[] | {user: .user.login, user_type: .user.type, "
        "created_at: .created_at, body: .body[0:300]}]",
        timeout=30
    ) or []
    reviews = run_gh(
        "api", f"repos/{repo}/pulls/{number}/reviews",
        "--jq=[.[] | {user: .user.login, user_type: .user.type, "
        "state: .state, submitted_at: .submitted_at, body: .body[0:300]}]",
        timeout=30
    ) or []
    return comments, reviews


def fetch_pr_checks(repo, number):
    """拉取 CI 检查状态。"""
    checks = run_gh(
        "pr", "checks", str(number),
        "-R", repo,
        "--json=name,state,startedAt,completedAt,link",
        timeout=30
    )
    if not checks:
        return []
    return checks


def is_bot(user, user_type=None):
    """判断是否为 bot 用户。"""
    if user in BOT_USERS:
        return True
    if user_type and user_type == "Bot":
        return True
    if user and user.endswith("[bot]"):
        return True
    return False


def analyze_pr_needs_action(pr, detail, comments, reviews, checks):
    """
    分析单个 PR 是否需要 action，以及原因。
    返回: (needs_action: bool, reasons: list[str], priority: 'high'|'medium'|'low')
    """
    reasons = []
    priority = "low"
    repo = pr["repository"]["nameWithOwner"]
    # Hermes-agent 是自家人，降级处理
    is_internal = repo == "NousResearch/hermes-agent"

    # 1. 检查 mergeable_state
    merge_state = detail.get("mergeStateStatus", "")
    mergeable = detail.get("mergeable", "")
    if merge_state == "DIRTY" or mergeable == "CONFLICTING":
        reasons.append("🔒 有 merge conflict，需要 rebase/resolve")
        priority = "high"
    elif merge_state == "BEHIND":
        reasons.append("📉 分支落后 main，需要 rebase")
        priority = "medium"
    elif merge_state == "BLOCKED":
        # BLOCKED 可能是 review/CI/branch protection，需要进一步判断
        pass

    # 2. 检查 reviewDecision
    review_decision = detail.get("reviewDecision", "")
    if review_decision == "REVIEW_REQUIRED":
        # 等待首次 review，如果超过 7 天才 ping
        created = pr.get("createdAt", "")
        if created:
            age_days = (datetime.now(timezone.utc) - 
                       datetime.fromisoformat(created.replace("Z", "+00:00"))).days
            if age_days > 7 and not is_internal:
                reasons.append(f"⏳ 等待 review 已 {age_days} 天，可考虑 ping")
                if priority == "low":
                    priority = "low"

    # 3. 检查是否有未回复的非 bot 评论
    last_human_comment = None
    last_human_review = None
    for c in comments:
        user = c.get("user", "")
        if user != SELF_USER and not is_bot(user, c.get("user_type")):
            last_human_comment = c
    for r in reviews:
        user = r.get("user", "")
        if user != SELF_USER and not is_bot(user, r.get("user_type")):
            last_human_review = r

    if last_human_comment:
        reasons.append(f"💬 有未回复评论 (by @{last_human_comment['user']})")
        if priority == "low":
            priority = "medium"

    # 4. CHANGES_REQUESTED 是最高优先级
    if last_human_review and last_human_review.get("state") == "CHANGES_REQUESTED":
        reasons = [f"📝 @{last_human_review['user']} 请求修改"] + reasons
        priority = "high"
    elif last_human_review and last_human_review.get("state") == "APPROVED":
        # 已 approved，可能只需要等 CI
        pass

    # 5. CI 失败检查
    failed_checks = []
    for chk in checks:
        if chk.get("state") in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            name = chk.get("name", "")
            # 忽略 Vercel/Netlify 部署失败（需要 owner 授权，不影响合并）
            if any(x in name for x in ["Vercel", "Netlify", "Deploy", "deploy"]):
                continue
            failed_checks.append(name)
    if failed_checks:
        reasons.append(f"❌ CI 失败: {', '.join(failed_checks[:3])}")
        if priority != "high":
            priority = "medium"

    # 内部仓库降级
    if is_internal and priority == "medium":
        priority = "low"

    needs_action = len(reasons) > 0
    return needs_action, reasons, priority


def build_pr_record(pr, fetch_detail=False):
    """构建单个 PR 的完整数据记录。"""
    repo = pr["repository"]["nameWithOwner"]
    number = pr["number"]
    state = pr.get("state", "").lower()
    record = {
        "repo": repo,
        "repo_short": repo.split("/")[-1],
        "repo_owner": repo.split("/")[0] if "/" in repo else "",
        "number": number,
        "title": pr.get("title", ""),
        "url": pr.get("url", ""),
        "state": state,
        "created_at": pr.get("createdAt", ""),
        "closed_at": pr.get("closedAt", "") if state != "open" else None,
        "updated_at": pr.get("updatedAt", ""),
        "needs_action": False,
        "action_reasons": [],
        "action_priority": "low",
        "merge_state": None,
        "review_decision": None,
        "is_draft": False,
        "additions": None,
        "deletions": None,
        "changed_files": None,
        "last_comment": None,
        "last_review": None,
        "failed_checks": [],
    }

    if not fetch_detail:
        return record

    # 只对 open PR 拉取详情（closed/merged 的 PR 不需要 action）
    if state != "open":
        return record

    print(f"  [detail] {repo} #{number}", file=sys.stderr)
    detail = fetch_pr_detail(repo, number)
    comments, reviews = fetch_pr_comments(repo, number)
    checks = fetch_pr_checks(repo, number)

    record["merge_state"] = detail.get("mergeStateStatus")
    record["review_decision"] = detail.get("reviewDecision")
    record["is_draft"] = detail.get("isDraft", False)
    record["additions"] = detail.get("additions")
    record["deletions"] = detail.get("deletions")
    record["changed_files"] = detail.get("changedFiles")

    # 找最后一条非 bot 非 self 评论/review
    for c in comments:
        user = c.get("user", "")
        if user != SELF_USER and not is_bot(user, c.get("user_type")):
            record["last_comment"] = {
                "user": user,
                "at": c.get("created_at", ""),
                "body": c.get("body", "")[:200],
            }
            break
    for r in reviews:
        user = r.get("user", "")
        if user != SELF_USER and not is_bot(user, r.get("user_type")):
            record["last_review"] = {
                "user": user,
                "state": r.get("state", ""),
                "at": r.get("submitted_at", ""),
                "body": r.get("body", "")[:200],
            }
            break

    # CI 失败检查列表
    failed = []
    for chk in checks:
        if chk.get("state") in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            name = chk.get("name", "")
            if not any(x in name for x in ["Vercel", "Netlify", "Deploy", "deploy"]):
                failed.append(name)
    record["failed_checks"] = failed

    # 分析 needs_action
    needs, reasons, priority = analyze_pr_needs_action(
        pr, detail, comments, reviews, checks
    )
    record["needs_action"] = needs
    record["action_reasons"] = reasons
    record["action_priority"] = priority

    return record


def compute_summary(prs):
    """计算总览统计。"""
    from collections import Counter
    total = len(prs)
    states = Counter(p["state"] for p in prs)
    open_count = states.get("open", 0)
    merged_count = states.get("merged", 0)
    closed_count = states.get("closed", 0)  # closed but not merged

    # 按仓库统计
    repo_stats = {}
    for p in prs:
        repo = p["repo"]
        if repo not in repo_stats:
            repo_stats[repo] = {"open": 0, "merged": 0, "closed": 0, "total": 0}
        repo_stats[repo][p["state"]] = repo_stats[repo].get(p["state"], 0) + 1
        repo_stats[repo]["total"] += 1

    # 时间趋势（按天）
    daily = {}
    for p in prs:
        date = p["created_at"][:10]
        if not date:
            continue
        if date not in daily:
            daily[date] = {"created": 0, "merged": 0, "closed": 0}
        daily[date]["created"] += 1
        if p["state"] == "merged" and p.get("closed_at"):
            merged_date = p["closed_at"][:10]
            if merged_date in daily:
                daily[merged_date]["merged"] += 1
        elif p["state"] == "closed" and p.get("closed_at"):
            closed_date = p["closed_at"][:10]
            if closed_date in daily:
                daily[closed_date]["closed"] += 1

    # 需要action的PR
    needs_action = [p for p in prs if p.get("needs_action")]
    high_priority = [p for p in needs_action if p.get("action_priority") == "high"]

    # 合并率
    merge_rate = (merged_count / total * 100) if total > 0 else 0

    return {
        "total": total,
        "open": open_count,
        "merged": merged_count,
        "closed": closed_count,
        "merge_rate": round(merge_rate, 1),
        "repo_count": len(repo_stats),
        "needs_action_count": len(needs_action),
        "high_priority_count": len(high_priority),
        "repo_stats": repo_stats,
        "daily": dict(sorted(daily.items())),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-only", action="store_true",
                       help="只拉取 open PR 详情（更快）")
    parser.add_argument("--output", default=str(OUTPUT_FILE),
                       help=f"输出文件 (默认: {OUTPUT_FILE})")
    args = parser.parse_args()

    # 确保 web 目录存在
    OUTPUT_FILE_PATH = Path(args.output)
    OUTPUT_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

    all_prs_raw = fetch_all_prs()
    if not all_prs_raw:
        print("[error] 没有获取到 PR 数据", file=sys.stderr)
        sys.exit(1)

    prs = []
    for i, pr in enumerate(all_prs_raw):
        # open PR 拉取详情，closed/merged 只用基础数据
        fetch_detail = pr.get("state", "").lower() == "open"
        if args.open_only and not fetch_detail:
            continue
        record = build_pr_record(pr, fetch_detail=fetch_detail)
        prs.append(record)
        # API 速率控制：每 10 个 PR 休息一下
        if fetch_detail and (i + 1) % 10 == 0:
            time.sleep(1)

    summary = compute_summary(prs)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "author": AUTHOR,
        "summary": summary,
        "prs": prs,
    }

    with open(OUTPUT_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n[done] 写入 {len(prs)} 个 PR 到 {OUTPUT_FILE_PATH}", file=sys.stderr)
    print(f"  Open: {summary['open']} | Merged: {summary['merged']} | "
          f"Closed: {summary['closed']} | Merge rate: {summary['merge_rate']}%",
          file=sys.stderr)
    print(f"  Needs action: {summary['needs_action_count']} "
          f"(high priority: {summary['high_priority_count']})", file=sys.stderr)


if __name__ == "__main__":
    main()
