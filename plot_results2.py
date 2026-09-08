#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

with open("batch_results.json", "r", encoding="utf-8") as f:
    data = json.load(f)

results = data["results"]
summary = data["summary"]

GNB_COORDS = {
    "2": (2000, 2000), "3": (3000, 2000), "4": (2500, 2866),
    "5": (1500, 2866), "6": (1000, 2000), "7": (1500, 1134), "8": (2500, 1134),
}

est_x = [r["estimated_x"] for r in results]
est_y = [r["estimated_y"] for r in results]
act_x = [r["actual_x"] for r in results]
act_y = [r["actual_y"] for r in results]
errors = [r["error_m"] for r in results]

fig, axes = plt.subplots(1, 2, figsize=(16, 7))

# ===== 그래프 1: 실제 vs 추정 위치 scatter =====
ax1 = axes[0]
ax1.set_facecolor("#f8f9fa")
ax1.grid(True, alpha=0.4, linestyle='--')

for ax, ay, ex, ey in zip(act_x, act_y, est_x, est_y):
    ax1.plot([ax, ex], [ay, ey], 'gray', alpha=0.25, linewidth=0.8)

ax1.scatter(act_x, act_y, c='#2196F3', s=60, zorder=5, label='Actual Position', alpha=0.85)
ax1.scatter(est_x, est_y, c='#FF5722', s=60, marker='^', zorder=5, label='Estimated Position', alpha=0.85)

for cid, (gx, gy) in GNB_COORDS.items():
    ax1.scatter(gx, gy, c='#4CAF50', s=180, marker='*', zorder=6)
    ax1.annotate(f'gNB {cid}', (gx, gy), textcoords="offset points",
                 xytext=(6, 6), fontsize=7, color='#2E7D32', fontweight='bold')

ax1.set_xlim(0, 4000)
ax1.set_ylim(0, 4000)
ax1.set_xlabel('X (m)', fontsize=11)
ax1.set_ylabel('Y (m)', fontsize=11)
ax1.set_title('Actual vs Estimated UE Position\n(SINR-Linear Weighted Centroid)', fontsize=12, fontweight='bold')
ax1.legend(loc='upper left', fontsize=9)
ax1.text(0.98, 0.02, f'Mean Error: {summary["mean_error_m"]}m\nRMSE: {summary["rmse_m"]}m\nN={summary["total"]}',
         transform=ax1.transAxes, ha='right', va='bottom', fontsize=9,
         bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))

# ===== 그래프 2: 오차 분포 히스토그램 =====
ax2 = axes[1]
ax2.set_facecolor("#f8f9fa")
ax2.grid(True, alpha=0.4, linestyle='--', axis='y')

n, bins, patches = ax2.hist(errors, bins=12, color='#5C6BC0', alpha=0.8, edgecolor='white', linewidth=0.8)

ax2.axvline(summary["mean_error_m"], color='#F44336', linewidth=2, linestyle='--',
            label=f'Mean: {summary["mean_error_m"]}m')
ax2.axvline(summary["rmse_m"], color='#FF9800', linewidth=2, linestyle='-.',
            label=f'RMSE: {summary["rmse_m"]}m')

ax2.set_xlabel('Localization Error (m)', fontsize=11)
ax2.set_ylabel('Count', fontsize=11)
ax2.set_title('Error Distribution\n(SINR-Linear Weighted Centroid)', fontsize=12, fontweight='bold')
ax2.legend(fontsize=9)

stats_text = (f'Min: {summary["min_error_m"]}m\n'
              f'Max: {summary["max_error_m"]}m\n'
              f'Mean: {summary["mean_error_m"]}m\n'
              f'RMSE: {summary["rmse_m"]}m')
ax2.text(0.97, 0.97, stats_text, transform=ax2.transAxes, ha='right', va='top',
         fontsize=9, bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))

plt.tight_layout(pad=2.0)
plt.savefig("localization_results.png", dpi=150, bbox_inches='tight')
print("저장 완료: localization_results.png")

# ===== 히스토그램 단독 저장 (논문용) =====
fig2, ax3 = plt.subplots(figsize=(8, 6))
ax3.set_facecolor("#f8f9fa")
ax3.grid(True, alpha=0.4, linestyle='--', axis='y')

ax3.hist(errors, bins=12, color='#5C6BC0', alpha=0.8, edgecolor='white', linewidth=0.8)
ax3.axvline(summary["mean_error_m"], color='#F44336', linewidth=2, linestyle='--',
            label=f'Mean: {summary["mean_error_m"]}m')
ax3.axvline(summary["rmse_m"], color='#FF9800', linewidth=2, linestyle='-.',
            label=f'RMSE: {summary["rmse_m"]}m')

ax3.set_xlabel('Localization Error (m)', fontsize=11)
ax3.set_ylabel('Count', fontsize=11)
ax3.set_title('Error Distribution\n(SINR-Linear Weighted Centroid)', fontsize=12, fontweight='bold')
ax3.legend(fontsize=9)
ax3.text(0.97, 0.97, stats_text, transform=ax3.transAxes, ha='right', va='top',
         fontsize=9, bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))

plt.tight_layout()
plt.savefig("error_histogram.png", dpi=150, bbox_inches='tight')
print("저장 완료: error_histogram.png")