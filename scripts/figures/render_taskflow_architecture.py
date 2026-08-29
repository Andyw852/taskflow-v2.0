#!/usr/bin/env python3
"""Render the publication figure for the taskflow workflow manager.

The composition uses three non-overlapping lanes:
1. supervision and safety policy;
2. a strictly left-to-right execution pipeline;
3. a dedicated failure/recovery lane.

Outputs
-------
* output/pdf/taskflow_architecture.pdf
* output/figures/taskflow_architecture.svg
* output/figures/taskflow_architecture.png  (rendered from the PDF with Poppler)
"""

import os
from pathlib import Path

import reportlab
from reportlab.graphics import renderPDF, renderSVG
from reportlab.graphics.shapes import Circle, Drawing, Line, Path as ShapePath, Polygon, Rect, String
from reportlab.lib.colors import HexColor
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


ROOT = Path(__file__).resolve().parents[2]
PDF_OUT = ROOT / "output" / "pdf" / "taskflow_architecture.pdf"
SVG_OUT = ROOT / "output" / "figures" / "taskflow_architecture.svg"
PNG_OUT = ROOT / "output" / "figures" / "taskflow_architecture.png"

W, H = 180 * mm, 105 * mm


COLORS = {
    "ink": HexColor("#17324D"),
    "muted": HexColor("#66788A"),
    "line": HexColor("#CAD5DF"),
    "line_dark": HexColor("#9FB0C0"),
    "paper": HexColor("#FFFFFF"),
    "surface": HexColor("#F7F9FB"),
    "surface_blue": HexColor("#F1F6FA"),
    "navy": HexColor("#214D6C"),
    "blue": HexColor("#3277A8"),
    "blue_soft": HexColor("#E8F1F8"),
    "teal": HexColor("#2A9D8F"),
    "teal_soft": HexColor("#E7F5F2"),
    "gold": HexColor("#D99A32"),
    "gold_soft": HexColor("#FFF4DD"),
    "coral": HexColor("#D86452"),
    "coral_soft": HexColor("#FCEDE9"),
    "green": HexColor("#27866F"),
    "green_soft": HexColor("#E8F4EF"),
    "gray_soft": HexColor("#EEF2F5"),
}


def register_figure_fonts():
    """Register embeddable fonts for publication-safe PDF output."""
    win_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    reportlab_fonts = Path(reportlab.__file__).resolve().parent / "fonts"
    candidates = [
        (win_fonts / "arial.ttf", win_fonts / "arialbd.ttf"),
        (Path("/mnt/c/Windows/Fonts/arial.ttf"), Path("/mnt/c/Windows/Fonts/arialbd.ttf")),
        (reportlab_fonts / "Vera.ttf", reportlab_fonts / "VeraBd.ttf"),
    ]
    for regular, bold in candidates:
        if regular.is_file() and bold.is_file():
            pdfmetrics.registerFont(TTFont("TaskflowSans", str(regular)))
            pdfmetrics.registerFont(TTFont("TaskflowSans-Bold", str(bold)))
            return
    raise RuntimeError("No embeddable sans-serif font was found")


register_figure_fonts()


def c(value):
    return COLORS[value] if isinstance(value, str) else value


def rounded(x, y, w, h, radius, fill="paper", stroke="line", sw=0.7):
    return Rect(
        x,
        y,
        w,
        h,
        rx=radius,
        ry=radius,
        fillColor=c(fill),
        strokeColor=c(stroke) if stroke is not None else None,
        strokeWidth=sw,
    )


def text(d, x, y, value, size=5, color="ink", bold=False, anchor="start"):
    d.add(
        String(
            x,
            y,
            value,
            fontName="TaskflowSans-Bold" if bold else "TaskflowSans",
            fontSize=size,
            fillColor=c(color),
            textAnchor=anchor,
        )
    )


def lines(d, x, y, values, size=4.8, leading=5.8, color="muted"):
    for i, value in enumerate(values):
        text(d, x, y - i * leading, value, size=size, color=color)


def arrow(d, x1, y1, x2, y2, color="navy", sw=1.25, head=1.8 * mm):
    """Draw a straight arrow; all figure routing uses dedicated straight lanes."""
    stroke = c(color)
    d.add(Line(x1, y1, x2, y2, strokeColor=stroke, strokeWidth=sw))
    dx, dy = x2 - x1, y2 - y1
    norm = max((dx * dx + dy * dy) ** 0.5, 1e-6)
    ux, uy = dx / norm, dy / norm
    px, py = -uy, ux
    bx, by = x2 - ux * head, y2 - uy * head
    d.add(
        Polygon(
            [x2, y2, bx + px * head * 0.46, by + py * head * 0.46, bx - px * head * 0.46, by - py * head * 0.46],
            fillColor=stroke,
            strokeColor=stroke,
        )
    )


def icon_person(d, cx, cy, color="blue"):
    stroke = c(color)
    d.add(Circle(cx, cy + 2.0 * mm, 1.45 * mm, fillColor=None, strokeColor=stroke, strokeWidth=0.8))
    p = ShapePath()
    p.moveTo(cx - 3.0 * mm, cy - 2.3 * mm)
    p.curveTo(cx - 2.2 * mm, cy + 0.2 * mm, cx + 2.2 * mm, cy + 0.2 * mm, cx + 3.0 * mm, cy - 2.3 * mm)
    p.fillColor = None
    p.strokeColor = stroke
    p.strokeWidth = 0.8
    d.add(p)


def icon_agent(d, cx, cy, color="teal"):
    stroke = c(color)
    d.add(rounded(cx - 3.0 * mm, cy - 2.2 * mm, 6.0 * mm, 4.8 * mm, 1.0 * mm, fill=None if False else "paper", stroke=color, sw=0.8))
    d.add(Circle(cx - 1.2 * mm, cy, 0.42 * mm, fillColor=stroke, strokeColor=stroke))
    d.add(Circle(cx + 1.2 * mm, cy, 0.42 * mm, fillColor=stroke, strokeColor=stroke))
    d.add(Line(cx, cy + 2.7 * mm, cx, cy + 3.6 * mm, strokeColor=stroke, strokeWidth=0.7))
    d.add(Circle(cx, cy + 3.8 * mm, 0.35 * mm, fillColor=stroke, strokeColor=stroke))


def icon_terminal(d, x, y, color="navy"):
    stroke = c(color)
    d.add(rounded(x, y, 7.0 * mm, 5.2 * mm, 0.8 * mm, fill="paper", stroke=color, sw=0.8))
    d.add(Line(x + 1.2 * mm, y + 3.4 * mm, x + 2.3 * mm, y + 2.6 * mm, strokeColor=stroke, strokeWidth=0.8))
    d.add(Line(x + 2.3 * mm, y + 2.6 * mm, x + 1.2 * mm, y + 1.8 * mm, strokeColor=stroke, strokeWidth=0.8))
    d.add(Line(x + 3.1 * mm, y + 1.7 * mm, x + 5.6 * mm, y + 1.7 * mm, strokeColor=stroke, strokeWidth=0.8))


def icon_shield(d, cx, cy, color="coral"):
    stroke = c(color)
    p = ShapePath()
    p.moveTo(cx, cy + 3.5 * mm)
    p.lineTo(cx + 3.1 * mm, cy + 2.2 * mm)
    p.lineTo(cx + 2.5 * mm, cy - 1.8 * mm)
    p.lineTo(cx, cy - 3.4 * mm)
    p.lineTo(cx - 2.5 * mm, cy - 1.8 * mm)
    p.lineTo(cx - 3.1 * mm, cy + 2.2 * mm)
    p.lineTo(cx, cy + 3.5 * mm)
    p.fillColor = c("coral_soft")
    p.strokeColor = stroke
    p.strokeWidth = 0.8
    d.add(p)
    d.add(Line(cx - 1.4 * mm, cy, cx - 0.2 * mm, cy - 1.2 * mm, strokeColor=stroke, strokeWidth=0.9))
    d.add(Line(cx - 0.2 * mm, cy - 1.2 * mm, cx + 1.8 * mm, cy + 1.4 * mm, strokeColor=stroke, strokeWidth=0.9))


def icon_file(d, x, y, color="blue"):
    stroke = c(color)
    d.add(Rect(x, y, 6.0 * mm, 7.5 * mm, fillColor=None, strokeColor=stroke, strokeWidth=0.8))
    for offset, length in [(5.4, 3.8), (3.8, 3.8), (2.2, 2.8)]:
        d.add(Line(x + 1.1 * mm, y + offset * mm, x + (1.1 + length) * mm, y + offset * mm, strokeColor=stroke, strokeWidth=0.65))


def icon_manifest(d, x, y, color="gold"):
    stroke = c(color)
    for i in range(3):
        yy = y + (5.3 - i * 2.2) * mm
        d.add(Circle(x + 0.8 * mm, yy, 0.52 * mm, fillColor=stroke, strokeColor=stroke))
        d.add(Line(x + 2.0 * mm, yy, x + 6.4 * mm, yy, strokeColor=stroke, strokeWidth=0.8))


def icon_servers(d, x, y, color="teal"):
    stroke = c(color)
    for i in range(3):
        yy = y + i * 2.7 * mm
        d.add(rounded(x, yy, 7.8 * mm, 2.05 * mm, 0.55 * mm, fill="paper", stroke=color, sw=0.65))
        d.add(Circle(x + 1.0 * mm, yy + 1.0 * mm, 0.27 * mm, fillColor=stroke, strokeColor=stroke))
        d.add(Line(x + 2.0 * mm, yy + 1.0 * mm, x + 6.7 * mm, yy + 1.0 * mm, strokeColor=stroke, strokeWidth=0.5))


def icon_check(d, cx, cy, color="green"):
    stroke = c(color)
    d.add(Circle(cx, cy, 3.1 * mm, fillColor=None, strokeColor=stroke, strokeWidth=0.85))
    d.add(Line(cx - 1.6 * mm, cy, cx - 0.35 * mm, cy - 1.25 * mm, strokeColor=stroke, strokeWidth=1.0))
    d.add(Line(cx - 0.35 * mm, cy - 1.25 * mm, cx + 1.9 * mm, cy + 1.5 * mm, strokeColor=stroke, strokeWidth=1.0))


def draw_control_plane(d):
    x, y, w, h = 7 * mm, 74 * mm, 166 * mm, 18 * mm
    d.add(rounded(x, y, w, h, 2.8 * mm, fill="surface_blue", stroke="line", sw=0.7))
    text(d, x + 3 * mm, y + h - 4.0 * mm, "SUPERVISION PLANE", size=5.0, color="navy", bold=True)

    col_w = w / 4
    for i in range(1, 4):
        xx = x + i * col_w
        d.add(Line(xx, y + 2.0 * mm, xx, y + h - 2.0 * mm, strokeColor=c("line"), strokeWidth=0.55))

    centers = [x + (i + 0.5) * col_w for i in range(4)]
    icon_person(d, centers[0] - 12 * mm, y + 7.2 * mm)
    text(d, centers[0] - 7 * mm, y + 8.0 * mm, "Human researcher", size=5.5, bold=True)
    text(d, centers[0] - 7 * mm, y + 4.2 * mm, "goals + authorization", size=4.45, color="muted")

    icon_agent(d, centers[1] - 12 * mm, y + 7.0 * mm)
    text(d, centers[1] - 7 * mm, y + 8.0 * mm, "LLM supervisor", size=5.5, bold=True)
    text(d, centers[1] - 7 * mm, y + 4.2 * mm, "monitor + diagnose", size=4.45, color="muted")

    icon_terminal(d, centers[2] - 15 * mm, y + 4.5 * mm)
    text(d, centers[2] - 6 * mm, y + 8.0 * mm, "Atomic tf interface", size=5.5, bold=True)
    text(d, centers[2] - 6 * mm, y + 4.2 * mm, "stable commands + exit codes", size=4.3, color="muted")

    icon_shield(d, centers[3] - 12.5 * mm, y + 7.0 * mm)
    text(d, centers[3] - 7 * mm, y + 8.0 * mm, "Safety policy", size=5.5, bold=True)
    text(d, centers[3] - 7 * mm, y + 4.2 * mm, "guard destructive actions", size=4.3, color="muted")


def stage_shell(d, x, y, w, h, accent, number, title_value, border="line"):
    d.add(rounded(x, y, w, h, 2.5 * mm, fill="paper", stroke=border, sw=0.8))
    d.add(Rect(x + 1.5 * mm, y + h - 2.2 * mm, w - 3 * mm, 2.2 * mm, fillColor=c(accent), strokeColor=None))
    d.add(Circle(x + 4.2 * mm, y + h - 5.8 * mm, 2.1 * mm, fillColor=c(accent), strokeColor=c(accent)))
    text(d, x + 4.2 * mm, y + h - 6.95 * mm, str(number), size=5.0, color="paper", bold=True, anchor="middle")
    text(d, x + 8.1 * mm, y + h - 7.0 * mm, title_value, size=5.55, color="ink", bold=True)


def draw_execution_pipeline(d):
    y, h = 36 * mm, 30 * mm
    stages = {
        "project": (7 * mm, 23 * mm),
        "skills": (35 * mm, 28 * mm),
        "engine": (68 * mm, 44 * mm),
        "hpc": (117 * mm, 24 * mm),
        "results": (146 * mm, 27 * mm),
    }

    # Main flow connectors are drawn first and occupy only the inter-card gaps.
    centers_y = y + h / 2
    ordered = [stages[k] for k in ["project", "skills", "engine", "hpc", "results"]]
    for (x1, w1), (x2, _) in zip(ordered, ordered[1:]):
        arrow(d, x1 + w1 + 0.7 * mm, centers_y, x2 - 0.7 * mm, centers_y, color="navy", sw=1.25, head=1.45 * mm)

    # Stage 1: project source.
    x, w = stages["project"]
    stage_shell(d, x, y, w, h, "blue", 1, "Material projects")
    icon_file(d, x + 3.3 * mm, y + 7.0 * mm)
    lines(d, x + 10.6 * mm, y + 18.3 * mm, ["POSCAR + tree", "project_setting/", "step.conf"], size=4.55, leading=5.5)
    text(d, x + 3.2 * mm, y + 3.3 * mm, "local source of truth", size=4.2, color="blue", bold=True)

    # Stage 2: skill manifests.
    x, w = stages["skills"]
    stage_shell(d, x, y, w, h, "gold", 2, "Workflow blueprint")
    icon_manifest(d, x + 3.5 * mm, y + 10.0 * mm)
    text(d, x + 11.3 * mm, y + 18.0 * mm, "skill.yaml", size=5.0, color="gold", bold=True)
    lines(d, x + 11.3 * mm, y + 13.6 * mm, ["VASP / DFT", "MACE / MLFF", "screening"], size=4.45, leading=5.1)
    text(d, x + 3.2 * mm, y + 3.3 * mm, "DAG + optional fanout", size=4.15, color="muted")

    # Stage 3: orchestration engine (the visual focal point).
    x, w = stages["engine"]
    stage_shell(d, x, y, w, h, "navy", 3, "taskflow engine", border="navy")
    engine_rows = [
        ("Discover materials", "blue"),
        ("Resolve configuration layers", "gold"),
        ("Generate inputs + dispatch", "teal"),
        ("Collect jobs + infer state", "green"),
    ]
    yy = y + 18.6 * mm
    for label, color in engine_rows:
        d.add(Circle(x + 4.5 * mm, yy + 0.55 * mm, 0.62 * mm, fillColor=c(color), strokeColor=c(color)))
        text(d, x + 7.0 * mm, yy, label, size=4.75, color="ink")
        yy -= 4.3 * mm
    text(d, x + w - 3.2 * mm, y + 3.1 * mm, "stateless reconstruction", size=4.15, color="navy", bold=True, anchor="end")

    # Stage 4: heterogeneous execution.
    x, w = stages["hpc"]
    stage_shell(d, x, y, w, h, "teal", 4, "HPC execution")
    icon_servers(d, x + 3.0 * mm, y + 8.2 * mm)
    text(d, x + 12.0 * mm, y + 18.5 * mm, "jzzn", size=4.8, bold=True)
    text(d, x + 12.0 * mm, y + 14.5 * mm, "A800", size=4.8, bold=True)
    text(d, x + 12.0 * mm, y + 10.5 * mm, "RTX3090", size=4.8, bold=True)
    text(d, x + 3.0 * mm, y + 3.2 * mm, "SLURM / fakeslurm", size=4.1, color="muted")

    # Stage 5: validation and synchronized results.
    x, w = stages["results"]
    stage_shell(d, x, y, w, h, "green", 5, "Validation + results")
    icon_check(d, x + 6.2 * mm, y + 13.0 * mm)
    lines(d, x + 11.3 * mm, y + 18.3 * mm, ["built-in criteria", "auto-fetch", "result/<step>"], size=4.45, leading=5.3)
    text(d, x + 3.2 * mm, y + 3.2 * mm, "verified artifacts only", size=4.1, color="green", bold=True)


def state_dot(d, x, y, label, color):
    d.add(Circle(x, y + 0.55 * mm, 0.75 * mm, fillColor=c(color), strokeColor=c(color)))
    text(d, x + 1.7 * mm, y, label, size=4.05, color="ink", bold=True)


def draw_feedback_lane(d):
    y, h = 6 * mm, 19 * mm
    state_x, state_w = 7 * mm, 49 * mm
    recovery_x, recovery_w = 62 * mm, 59 * mm
    failure_x, failure_w = 127 * mm, 46 * mm

    # State vocabulary.
    d.add(rounded(state_x, y, state_w, h, 2.3 * mm, fill="surface", stroke="line", sw=0.65))
    text(d, state_x + 3 * mm, y + h - 4.3 * mm, "OBSERVABLE STATE MODEL", size=4.8, color="navy", bold=True)
    top_states = [("PREP", "line_dark"), ("TODO", "blue"), ("PD", "gold"), ("R", "teal"), ("OK", "green")]
    xx = state_x + 3.3 * mm
    for label, color in top_states:
        state_dot(d, xx, y + 8.8 * mm, label, color)
        xx += (8.4 if label in {"PREP", "TODO"} else 6.9) * mm
    bottom_states = [("FAIL", "coral"), ("WAIT", "line_dark"), ("SCANCEL", "coral")]
    xx = state_x + 3.3 * mm
    for label, color in bottom_states:
        state_dot(d, xx, y + 3.6 * mm, label, color)
        xx += (9.6 if label == "SCANCEL" else 8.8) * mm

    # Recovery path.
    d.add(rounded(recovery_x, y, recovery_w, h, 2.3 * mm, fill="gold_soft", stroke="gold", sw=0.7))
    text(d, recovery_x + 3 * mm, y + h - 4.3 * mm, "RECOVERY PATH", size=4.8, color="gold", bold=True)
    text(d, recovery_x + 3 * mm, y + 9.0 * mm, "diagnose cause  ->  authorize action  ->  retry", size=4.65, color="ink", bold=True)
    text(d, recovery_x + 3 * mm, y + 4.2 * mm, "existing files are preserved; rerun rebuilds inputs", size=4.25, color="muted")

    # Failure monitoring.
    d.add(rounded(failure_x, y, failure_w, h, 2.3 * mm, fill="coral_soft", stroke="coral", sw=0.7))
    text(d, failure_x + 3 * mm, y + h - 4.3 * mm, "FAILURE + HANG MONITOR", size=4.8, color="coral", bold=True)
    text(d, failure_x + 3 * mm, y + 9.0 * mm, "FAIL / SCANCEL / stale output", size=4.5, color="ink", bold=True)
    text(d, failure_x + 3 * mm, y + 4.2 * mm, "progress fingerprint | dry-run", size=4.25, color="muted")

    # The recovery loop is confined to this bottom lane.
    arrow(d, 159.5 * mm, 36 * mm, 159.5 * mm, y + h, color="coral", sw=1.05, head=1.55 * mm)
    text(d, 161.2 * mm, 30.0 * mm, "failure", size=4.0, color="coral", bold=True)
    arrow(d, failure_x, y + 10.2 * mm, recovery_x + recovery_w, y + 10.2 * mm, color="coral", sw=1.05, head=1.55 * mm)
    arrow(d, 90.0 * mm, y + h, 90.0 * mm, 36 * mm, color="gold", sw=1.05, head=1.55 * mm)
    text(d, 92.0 * mm, 30.0 * mm, "authorized retry", size=4.0, color="gold", bold=True)


def build():
    d = Drawing(W, H)
    d.add(Rect(0, 0, W, H, fillColor=c("paper"), strokeColor=None))

    text(d, 7 * mm, 99.2 * mm, "taskflow: supervised atomistic workflows across heterogeneous HPC", size=10.2, color="ink", bold=True)
    text(
        d,
        7 * mm,
        95.5 * mm,
        "Local projects -> self-describing workflows -> task orchestration -> compute resources -> verified results",
        size=5.15,
        color="muted",
    )

    draw_control_plane(d)
    draw_execution_pipeline(d)
    draw_feedback_lane(d)

    # A single vertical command path links the control and execution planes.
    arrow(d, 90.0 * mm, 74 * mm, 90.0 * mm, 66 * mm, color="navy", sw=1.15, head=1.6 * mm)
    text(d, 92.0 * mm, 69.1 * mm, "tf calls", size=4.0, color="navy", bold=True)
    return d


def main():
    PDF_OUT.parent.mkdir(parents=True, exist_ok=True)
    SVG_OUT.parent.mkdir(parents=True, exist_ok=True)
    drawing = build()
    renderPDF.drawToFile(drawing, str(PDF_OUT), "taskflow supervised workflow architecture")
    renderSVG.drawToFile(drawing, str(SVG_OUT))

    # Keep SVG text editable and portable outside the ReportLab font registry.
    svg = SVG_OUT.read_text(encoding="utf-8")
    svg = svg.replace(
        "font-family: TaskflowSans-Bold;",
        "font-family: Arial, Helvetica, sans-serif; font-weight: bold;",
    ).replace(
        "font-family: TaskflowSans;",
        "font-family: Arial, Helvetica, sans-serif;",
    )
    SVG_OUT.write_text(svg, encoding="utf-8")
    print(PDF_OUT)
    print(SVG_OUT)
    print(f"Render {PNG_OUT.name} from the PDF at 600 dpi with pdftoppm.")


if __name__ == "__main__":
    main()
