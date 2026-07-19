# PR Dashboard

实时监控 [sergioperezcheco](https://github.com/sergioperezcheco) 的开源 PR 贡献情况。

## 结构

```
pr-dashboard/
├── scripts/
│   └── collect_pr_data.py   # 数据收集脚本（gh CLI → data.json）
├── web/
│   ├── index.html           # 单页前端（Chart.js + Tailwind CDN）
│   └── data.json            # 最新 PR 数据（自动更新）
├── docs/
│   └── README.md            # 本文档
└── .github/workflows/       # GitHub Actions（可选自动更新）
```

## 本地预览

```bash
cd web && python3 -m http.server 8765
# 浏览器打开 http://localhost:8765
```

## 更新数据

```bash
python3 scripts/collect_pr_data.py
```

## 监控维度

- **总览 KPI**：总数、Open/Merged/Closed、合并率、需处理数
- **时间趋势**：按天的创建/合并/关闭量
- **仓库分布**：各仓库的 PR 数量与状态比例
- **需要 Action**：自动识别需要回复/rebase/修 CI 的 PR
- **PR 明细列表**：支持搜索和过滤

## 自动化

数据通过 Hermes cron job 每 30 分钟更新（08:00-21:30 运行窗口）。
