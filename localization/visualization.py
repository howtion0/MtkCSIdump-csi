"""Dependency-free SVG rendering for the explicitly synthetic demo."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .fusion import APObservation, GridSupport
from .simulate import SYNTHETIC_NOTICE


def render_synthetic_room_svg(
    support_result: GridSupport,
    observations: list[APObservation],
    true_position_m: tuple[float, float],
    output_path: str | Path,
) -> None:
    margin_x, margin_y = 90, 110
    room_w, room_h = 820, 480
    x_min, x_max = float(support_result.x_m.min()), float(support_result.x_m.max())
    y_min, y_max = float(support_result.y_m.min()), float(support_result.y_m.max())

    def project(point: tuple[float, float]) -> tuple[float, float]:
        x, y = point
        px = margin_x + (x - x_min) / (x_max - x_min) * room_w
        py = margin_y + room_h - (y - y_min) / (y_max - y_min) * room_h
        return px, py

    support = support_result.fused_normalized_support
    threshold = float(np.quantile(support, 0.84))
    peak = float(support.max())
    elements = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="680" viewBox="0 0 1000 680">',
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#071425"/><stop offset="1" stop-color="#101d35"/></linearGradient><filter id="glow"><feGaussianBlur stdDeviation="8" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>',
        '<rect width="1000" height="680" fill="url(#bg)"/>',
        '<text x="50" y="52" fill="#e7f6ff" font-size="25" font-family="system-ui" font-weight="700">AX3000T CSI — synthetic coarse-location support heatmap</text>',
        f'<rect x="35" y="64" width="930" height="34" rx="7" fill="#7b1f1f"/><text x="50" y="88" fill="#fff4d6" font-size="19" font-family="monospace" font-weight="800">{SYNTHETIC_NOTICE}</text>',
        f'<rect x="{margin_x}" y="{margin_y}" width="{room_w}" height="{room_h}" rx="14" fill="#0a182b" stroke="#4e6d91" stroke-width="2"/>',
    ]
    for x in np.linspace(x_min, x_max, 7):
        px, _ = project((float(x), y_min))
        elements.append(
            f'<line x1="{px:.1f}" y1="{margin_y}" x2="{px:.1f}" y2="{margin_y + room_h}" stroke="#17304c" stroke-width="1"/>'
        )
    for y in np.linspace(y_min, y_max, 5):
        _, py = project((x_min, float(y)))
        elements.append(
            f'<line x1="{margin_x}" y1="{py:.1f}" x2="{margin_x + room_w}" y2="{py:.1f}" stroke="#17304c" stroke-width="1"/>'
        )

    x_mesh, y_mesh = np.meshgrid(support_result.x_m, support_result.y_m)
    for x, y, value in zip(x_mesh.ravel(), y_mesh.ravel(), support.ravel()):
        if value < threshold:
            continue
        px, py = project((float(x), float(y)))
        opacity = 0.05 + 0.55 * float(value / max(peak, 1e-15))
        elements.append(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="10" fill="#00d9ff" opacity="{opacity:.3f}"/>'
        )

    estimate_px, estimate_py = project(support_result.display_peak_position_m)
    true_px, true_py = project(true_position_m)
    for observation in observations:
        apx, apy = project(observation.position_m)
        elements.extend(
            [
                f'<line x1="{apx:.1f}" y1="{apy:.1f}" x2="{estimate_px:.1f}" y2="{estimate_py:.1f}" stroke="#66e3ff" stroke-width="3" stroke-dasharray="10 8" opacity="0.72"/>',
                f'<circle cx="{apx:.1f}" cy="{apy:.1f}" r="18" fill="#692cff" stroke="#d7c9ff" stroke-width="3" filter="url(#glow)"/>',
                f'<text x="{apx + 25:.1f}" y="{apy + 5:.1f}" fill="#e8e1ff" font-size="15" font-family="system-ui">{observation.receiver_id}</text>',
            ]
        )
    elements.extend(
        [
            f'<circle cx="{true_px:.1f}" cy="{true_py:.1f}" r="10" fill="none" stroke="#7cff8d" stroke-width="4"/>',
            f'<text x="{true_px + 16:.1f}" y="{true_py - 12:.1f}" fill="#9effaa" font-size="14" font-family="system-ui">synthetic truth</text>',
            f'<circle cx="{estimate_px:.1f}" cy="{estimate_py:.1f}" r="22" fill="#00d9ff" opacity="0.30" filter="url(#glow)"/>',
            f'<circle cx="{estimate_px:.1f}" cy="{estimate_py:.1f}" r="7" fill="#ffffff"/>',
            f'<text x="{estimate_px + 16:.1f}" y="{estimate_py + 24:.1f}" fill="#dffaff" font-size="14" font-family="system-ui">display peak {support_result.display_peak_position_m[0]:.2f}, {support_result.display_peak_position_m[1]:.2f} m</text>',
            '<text x="90" y="625" fill="#91a8c4" font-size="14" font-family="system-ui">Output: normalized support heatmap / coarse sectors / ambiguity flags — not a body outline or calibrated location probability.</text>',
            f'<text x="90" y="650" fill="#91a8c4" font-size="14" font-family="system-ui">80% display-mass radius {support_result.display_mass_radius_80_m:.2f} m · evidence score {support_result.evidence.score:.2f} (quality indicator)</text>',
            "</svg>",
        ]
    )
    Path(output_path).write_text("\n".join(elements) + "\n", encoding="utf-8")
