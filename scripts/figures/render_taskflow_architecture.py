#!/usr/bin/env python3
"""Render the publication figure for the taskflow workflow manager.

The figure is deliberately organized into two visual zones:
1. a compact system architecture with a clear top-to-bottom hierarchy;
2. a terminal-style command contract with representative, valid tf commands.

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
from reportlab.graphics.shapes import Circle, Drawing, Line, Polygon, Rect, String
from reportlab.lib.colors import HexColor
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


ROOT = Path(__file__).resolve().parents[2]
PDF_OUT = ROOT / "output" / "pdf" / "taskflow_architecture.pdf"
SVG_OUT = ROOT / "output" / "figures" / "taskflow_architecture.svg"
PNG_OUT = ROOT / "output" / "figures" / "taskflow_architecture.png"

# A full-width journal figure with enough height for readable annotations.
W, H = 180 * mm, 118 * mm
TEXT_SCALE = 1.0


COLORS = {
    "ink": HexColor("#18324B"),
    "muted": HexColor("#667A8D"),
    "faint": HexColor("#91A2B3"),
    "line": HexColor("#C7D3DE"),
    "line_dark": HexColor("#9FB1C2"),
    "paper": HexColor("#FFFFFF"),
    "surface": HexColor("#F7F9FC"),
    "lavender": HexColor("#F0EFFA"),
    "navy": HexColor("#17344E"),
    "navy_deep": HexColor("#0E2638"),
    "blue": HexColor("#2D78AA"),
    "blue_soft": HexColor("#E9F2F8"),
    "purple": HexColor("#6654B5"),
    "teal": HexColor("#239B8C"),
    "teal_soft": HexColor("#E8F6F3"),
    "gold": HexColor("#C78920"),
    "gold_soft": HexColor("#FFF4DC"),
    "coral": HexColor("#C8513A"),
    "coral_soft": HexColor("#FCECE7"),
    "green": HexColor("#2E8B73"),
    "terminal_text": HexColor("#D7E7E8"),
    "terminal_muted": HexColor("#8FB0B3"),
    "terminal_gold": HexColor("#F5C34E"),
    "terminal_green": HexColor("#91D4C0"),
}


def register_figure_fonts():
    """Register embeddable sans-serif and monospaced fonts."""
    win_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    reportlab_fonts = Path(reportlab.__file__).resolve().parent / "fonts"

    sans_candidates = [
        (win_fonts / "arial.ttf", win_fonts / "arialbd.ttf"),
        (Path("/mnt/c/Windows/Fonts/arial.ttf"), Path("/mnt/c/Windows/Fonts/arialbd.ttf")),
        (reportlab_fonts / "Vera.ttf", reportlab_fonts / "VeraBd.ttf"),
    ]
    mono_candidates = [
        (win_fonts / "consola.ttf", win_fonts / "consolab.ttf"),
        (Path("/mnt/c/Windows/Fonts/consola.ttf"), Path("/mnt/c/Windows/Fonts/consolab.ttf")),
        (reportlab_fonts / "VeraMono.ttf", reportlab_fonts / "VeraMoBd.ttf"),
    ]

    for regular, bold in sans_candidates:
        if regular.is_file() and bold.is_file():
            pdfmetrics.registerFont(TTFont("TaskflowSans", str(regular)))
            pdfmetrics.registerFont(TTFont("TaskflowSans-Bold", str(bold)))
            break
    else:
        raise RuntimeError("No embeddable sans-serif font was found")

    for regular, bold in mono_candidates:
        if regular.is_file() and bold.is_file():
            pdfmetrics.registerFont(TTFont("TaskflowMono", str(regular)))
            pdfmetrics.registerFont(TTFont("TaskflowMono-Bold", str(bold)))
            break
    else:
        raise RuntimeError("No embeddable monospaced font was found")


register_figure_fonts()


def color(value):
    if value is None:
        return None
    return COLORS[value] if isinstance(value, str) else value


def rounded(x, y, w, h, radius, fill="paper", stroke="line", sw=0.65):
    return Rect(
        x,
        y,
        w,
        h,
        rx=radius,
        ry=radius,
        fillColor=color(fill),
        strokeColor=color(stroke),
        strokeWidth=sw,
    )


def text(d, x, y, value, size=5.0, fill="ink", bold=False, anchor="start", mono=False):
    if mono:
        font = "TaskflowMono-Bold" if bold else "TaskflowMono"
    else:
        font = "TaskflowSans-Bold" if bold else "TaskflowSans"
    d.add(
        String(
            x,
            y,
            value,
            fontName=font,
            fontSize=size * TEXT_SCALE,
            fillColor=color(fill),
            textAnchor=anchor,
        )
    )


def arrow(d, x1, y1, x2, y2, fill="navy", sw=1.05, head=1.45 * mm):
    """Draw a short straight arrow; routes never pass through text."""
    stroke = color(fill)
    d.add(Line(x1, y1, x2, y2, strokeColor=stroke, strokeWidth=sw))
    dx, dy = x2 - x1, y2 - y1
    norm = max((dx * dx + dy * dy) ** 0.5, 1e-6)
    ux, uy = dx / norm, dy / norm
    px, py = -uy, ux
    bx, by = x2 - ux * head, y2 - uy * head
    d.add(
        Polygon(
            [
                x2,
                y2,
                bx + px * head * 0.43,
                by + py * head * 0.43,
                bx - px * head * 0.43,
                by - py * head * 0.43,
            ],
            fillColor=stroke,
            strokeColor=stroke,
        )
    )


def section_shell(d, x, y, w, h, title_value, accent, fill="paper"):
    d.add(rounded(x, y, w, h, 2.2 * mm, fill=fill, stroke="line", sw=0.7))
    d.add(Rect(x, y + h - 1.25 * mm, w, 1.25 * mm, fillColor=color(accent), strokeColor=None))
    text(d, x + 3.0 * mm, y + h - 4.4 * mm, title_value, size=5.7, fill="ink", bold=True)


def info_card(d, x, y, w, h, title_value, subtitle, accent, fill="paper"):
    d.add(rounded(x, y, w, h, 1.65 * mm, fill=fill, stroke="line", sw=0.6))
    d.add(Rect(x, y, 1.15 * mm, h, fillColor=color(accent), strokeColor=None))
    text(d, x + 3.0 * mm, y + h - 3.7 * mm, title_value, size=4.75, fill="ink", bold=True)
    if isinstance(subtitle, (tuple, list)):
        text(d, x + 3.0 * mm, y + 3.35 * mm, subtitle[0], size=3.35, fill="muted")
        text(d, x + 3.0 * mm, y + 1.25 * mm, subtitle[1], size=3.35, fill="muted")
    else:
        text(d, x + 3.0 * mm, y + 2.25 * mm, subtitle, size=3.55, fill="muted")


def draw_supervision(d, x, y, w, h):
    section_shell(d, x, y, w, h, "Human-in-the-loop supervision", "purple", fill="lavender")
    gap = 2.0 * mm
    card_y = y + 1.4 * mm
    card_h = 7.8 * mm
    card_w = (w - 6.0 * mm - 2 * gap) / 3
    cards = [
        ("Human researcher", "defines goals and authorizes changes", "blue"),
        ("LLM supervisor", "monitors state and diagnoses failures", "purple"),
        ("AGENTS.md policy", "constrains actions and routes them through tf", "coral"),
    ]
    for i, (title_value, subtitle, accent) in enumerate(cards):
        xx = x + 3.0 * mm + i * (card_w + gap)
        info_card(d, xx, card_y, card_w, card_h, title_value, subtitle, accent)


def draw_control_exchange(d, x, y, w, h):
    """Two clean, separated channels between supervision and orchestration."""
    down_x = x + 36.5 * mm
    up_x = x + 74.5 * mm
    arrow(d, down_x, y + h, down_x, y, fill="coral", sw=0.95, head=1.15 * mm)
    arrow(d, up_x, y, up_x, y + h, fill="teal", sw=0.95, head=1.15 * mm)
    text(
        d,
        down_x - 2.6 * mm,
        y + 1.75 * mm,
        "authorized tf commands",
        size=3.55,
        fill="coral",
        bold=True,
        anchor="end",
    )
    text(
        d,
        up_x + 2.6 * mm,
        y + 1.75 * mm,
        "state + exit-code feedback",
        size=3.55,
        fill="teal",
        bold=True,
    )


def skill_box(d, x, y, w, h, title_value, subtitle, accent, fill):
    d.add(rounded(x, y, w, h, 1.25 * mm, fill=fill, stroke=accent, sw=0.7))
    text(d, x + w / 2, y + h - 2.7 * mm, title_value, size=4.0, fill="ink", bold=True, anchor="middle")
    text(d, x + w / 2, y + 1.35 * mm, subtitle, size=2.9, fill=accent, anchor="middle")


def ellipsis_box(d, x, y, w, h, accent):
    """An open-ended pool item that signals additional and future skills."""
    d.add(rounded(x, y, w, h, 1.25 * mm, fill="surface", stroke="line_dark", sw=0.65))
    text(d, x + w / 2, y + h - 2.25 * mm, "...", size=6.2, fill=accent, bold=True, anchor="middle")
    text(d, x + w / 2, y + 1.15 * mm, "more skills", size=2.75, fill="muted", anchor="middle")


def draw_engine(d, x, y, w, h):
    section_shell(d, x, y, w, h, "taskflow orchestration engine", "navy", fill="surface")

    # Three primary responsibilities with short arrows confined to the gaps.
    gap = 2.2 * mm
    row_x = x + 3.0 * mm
    row_y = y + h - 15.5 * mm
    row_h = 9.2 * mm
    row_w = (w - 6.0 * mm - 2 * gap) / 3
    stages = [
        ("Material projects", "local structures + project configuration", "blue"),
        ("Skill pool", "versioned modules + validation criteria", "gold"),
        ("Orchestration loop", ("generate -> submit -> monitor", "collect -> validate -> advance"), "teal"),
    ]
    for i, (title_value, subtitle, accent) in enumerate(stages):
        xx = row_x + i * (row_w + gap)
        info_card(d, xx, row_y, row_w, row_h, title_value, subtitle, accent)
        if i < 2:
            arrow(
                d,
                xx + row_w + 0.35 * mm,
                row_y + row_h / 2,
                xx + row_w + gap - 0.35 * mm,
                row_y + row_h / 2,
                fill="navy",
                sw=0.8,
                head=1.0 * mm,
            )

    # The pool shows representative modules while remaining explicitly open-ended.
    cat_x = x + 3.0 * mm
    cat_y = y + 2.7 * mm
    cat_w = w - 6.0 * mm
    cat_h = 18.2 * mm
    d.add(rounded(cat_x, cat_y, cat_w, cat_h, 1.7 * mm, fill="paper", stroke="line", sw=0.6))
    text(d, cat_x + 2.5 * mm, cat_y + cat_h - 3.8 * mm, "Extensible skill pool", size=4.55, fill="ink", bold=True)
    text(
        d,
        cat_x + cat_w - 2.5 * mm,
        cat_y + cat_h - 3.8 * mm,
        "representative skill.yaml modules",
        size=3.35,
        fill="muted",
        anchor="end",
    )

    label_w = 7.0 * mm
    box_gap = 1.3 * mm
    usable_w = cat_w - label_w - 4.5 * mm

    dft_y = cat_y + 7.2 * mm
    dft_h = 5.5 * mm
    text(d, cat_x + 2.5 * mm, dft_y + 1.7 * mm, "DFT", size=3.7, fill="blue", bold=True)
    dft = [
        ("opt-dft", "relax"),
        ("band-dft", "bands"),
        ("ke-dft", "e-thermal"),
        ("kl-dft", "lattice k"),
    ]
    dft_w = (usable_w - 4 * box_gap) / 5
    start_x = cat_x + label_w + 2.0 * mm
    for i, (title_value, subtitle) in enumerate(dft):
        skill_box(d, start_x + i * (dft_w + box_gap), dft_y, dft_w, dft_h, title_value, subtitle, "blue", "blue_soft")
    ellipsis_box(d, start_x + 4 * (dft_w + box_gap), dft_y, dft_w, dft_h, "blue")

    mace_y = cat_y + 1.2 * mm
    mace_h = 5.3 * mm
    text(d, cat_x + 2.5 * mm, mace_y + 1.6 * mm, "MACE", size=3.7, fill="teal", bold=True)
    mace = [
        ("opt-mace", "CPU / GPU"),
        ("kl-mace", "CPU / GPU"),
        ("phonon-mace", "dispersion"),
        ("mlff-mace", "MLFF training"),
    ]
    mace_w = (usable_w - 4 * box_gap) / 5
    for i, (title_value, subtitle) in enumerate(mace):
        skill_box(d, start_x + i * (mace_w + box_gap), mace_y, mace_w, mace_h, title_value, subtitle, "teal", "teal_soft")
    ellipsis_box(d, start_x + 4 * (mace_w + box_gap), mace_y, mace_w, mace_h, "teal")

    # One short vertical link communicates that skills drive the loop.
    arrow(
        d,
        row_x + row_w * 1.5 + gap,
        row_y,
        row_x + row_w * 1.5 + gap,
        cat_y + cat_h + 0.6 * mm,
        fill="gold",
        sw=0.8,
        head=1.0 * mm,
    )


def cluster_card(d, x, y, w, h, title_value, subtitle, accent):
    d.add(rounded(x, y, w, h, 1.3 * mm, fill="paper", stroke="line", sw=0.55))
    d.add(Rect(x, y, 1.1 * mm, h, fillColor=color(accent), strokeColor=None))
    text(d, x + 3.0 * mm, y + h - 2.65 * mm, title_value, size=4.25, fill="ink", bold=True)
    text(d, x + 3.0 * mm, y + 1.15 * mm, subtitle, size=3.35, fill="muted")


def draw_hpc(d, x, y, w, h):
    section_shell(d, x, y, w, h, "HPC execution pools", "gold", fill="gold_soft")
    gap = 2.4 * mm
    card_x = x + 3.0 * mm
    card_y = y + 1.55 * mm
    card_h = 5.75 * mm
    card_w = (w - 6.0 * mm - gap) / 2
    clusters = [
        ("CPU cluster", "VASP / DFT + CPU MACE workloads", "teal"),
        ("GPU cluster", "GPU MACE + MLFF training workloads", "purple"),
    ]
    for i, (title_value, subtitle, accent) in enumerate(clusters):
        cluster_card(d, card_x + i * (card_w + gap), card_y, card_w, card_h, title_value, subtitle, accent)


def state_chip(d, x, y, label, fill):
    d.add(Circle(x, y + 0.5 * mm, 0.68 * mm, fillColor=color(fill), strokeColor=color(fill)))
    text(d, x + 1.55 * mm, y, label, size=3.55, fill="ink", bold=True)


def monitor_card(d, x, y, w, h, title_value, body, accent):
    d.add(rounded(x, y, w, h, 1.25 * mm, fill="paper", stroke="line", sw=0.55))
    d.add(Rect(x, y, 1.0 * mm, h, fillColor=color(accent), strokeColor=None))
    text(d, x + 3.0 * mm, y + h - 3.0 * mm, title_value, size=3.95, fill="ink", bold=True)
    text(d, x + 3.0 * mm, y + 1.35 * mm, body, size=3.55, fill="ink")


def draw_recovery(d, x, y, w, h):
    section_shell(d, x, y, w, h, "State-aware monitoring and recovery", "coral", fill="coral_soft")

    states = [
        ("PREP", "line_dark", 9.0),
        ("TODO", "blue", 9.0),
        ("PD", "gold", 6.8),
        ("R", "teal", 5.0),
        ("OK", "green", 6.6),
        ("FAIL", "coral", 8.0),
        ("WAIT", "line_dark", 8.5),
        ("SCANCEL", "coral", 12.0),
    ]
    xx = x + 24.5 * mm
    for label, fill, advance in states:
        state_chip(d, xx, y + h - 8.7 * mm, label, fill)
        xx += advance * mm

    card_gap = 2.0 * mm
    card_x = x + 3.0 * mm
    card_y = y + 1.5 * mm
    card_h = 7.2 * mm
    card_w = (w - 6.0 * mm - 2 * card_gap) / 3
    cards = [
        ("Auto advance", "fetch results and advance ready steps", "teal"),
        ("Hang detection", "fingerprint stalled output (dry-run)", "gold"),
        ("Quiet monitoring", "zero output when state is unchanged", "blue"),
    ]
    for i, (title_value, body, accent) in enumerate(cards):
        monitor_card(d, card_x + i * (card_w + card_gap), card_y, card_w, card_h, title_value, body, accent)


def terminal_line(d, x, y, value, fill="terminal_text", size=3.55, bold=False):
    text(d, x, y, value, size=size, fill=fill, bold=bold, mono=True)


def terminal_group(d, x, y, number, title_value, commands, note=None):
    terminal_line(d, x, y, f"{number:02d}  {title_value.upper()}", fill="terminal_gold", size=3.65, bold=True)
    yy = y - 5.0 * mm
    for command in commands:
        terminal_line(d, x, yy, "$", fill="terminal_green", size=3.55, bold=True)
        terminal_line(d, x + 3.0 * mm, yy, command, fill="terminal_text", size=3.45)
        yy -= 4.3 * mm
    if note:
        terminal_line(d, x, yy + 0.2 * mm, f"# {note}", fill="terminal_muted", size=3.1)
        yy -= 3.6 * mm
    return yy


def draw_terminal(d, x, y, w, h):
    d.add(rounded(x, y, w, h, 2.5 * mm, fill="navy_deep", stroke="navy_deep", sw=0.7))
    header_h = 10.2 * mm
    d.add(rounded(x, y + h - header_h, w, header_h, 2.5 * mm, fill="navy", stroke="navy", sw=0))
    d.add(Rect(x, y + h - header_h, w, 2.6 * mm, fillColor=color("navy"), strokeColor=None))
    # Restore a crisp outer edge after composing the rounded header.
    d.add(rounded(x, y, w, h, 2.5 * mm, fill=None, stroke="navy_deep", sw=0.7))

    for i, fill in enumerate(["coral", "terminal_gold", "green"]):
        d.add(Circle(x + (4.5 + i * 3.2) * mm, y + h - 5.1 * mm, 0.82 * mm, fillColor=color(fill), strokeColor=None))
    terminal_line(d, x + 17.0 * mm, y + h - 6.35 * mm, "REPRESENTATIVE TF COMMANDS", fill="terminal_text", size=3.75, bold=True)

    tx = x + 4.3 * mm
    yy = y + h - 15.0 * mm
    yy = terminal_group(d, tx, yy, 1, "initialize a project", ["tf -tt opt-mace-cpu -p MAT init"])
    yy -= 2.0 * mm
    yy = terminal_group(d, tx, yy, 2, "start or advance", ["tf -tt opt-mace-cpu -p MAT start", "tf start"])
    yy -= 2.0 * mm
    yy = terminal_group(
        d,
        tx,
        yy,
        3,
        "inspect (read-only)",
        ["tf summary --diff", "tf -tt kl-dft-cpu summary", "tf -tt band-dft-cpu -p C24/qHPC24 status"],
    )
    yy -= 2.0 * mm
    yy = terminal_group(
        d,
        tx,
        yy,
        4,
        "recover existing files",
        ["tf -tt band-dft-cpu -p C24/qHPC24 retry"],
        note="stop / rerun / clean require authorization",
    )
    yy -= 2.0 * mm
    terminal_group(
        d,
        tx,
        yy,
        5,
        "route future jobs",
        ["tf -p MAT hpc <cluster>"],
    )


def build():
    d = Drawing(W, H)
    d.add(Rect(0, 0, W, H, fillColor=color("paper"), strokeColor=None))

    # Title and one-line positioning statement.
    text(
        d,
        7.0 * mm,
        112.7 * mm,
        "taskflow: an LLM-supervised VASP/MACE workflow manager for heterogeneous HPC",
        size=9.15,
        fill="ink",
        bold=True,
    )
    text(
        d,
        7.0 * mm,
        108.5 * mm,
        "Human authorization and explicit policy govern a reproducible, state-aware execution engine.",
        size=4.45,
        fill="muted",
    )

    left_x, left_w = 7.0 * mm, 111.0 * mm
    terminal_x, terminal_w = 122.0 * mm, 51.0 * mm

    draw_supervision(d, left_x, 89.3 * mm, left_w, 15.2 * mm)
    draw_control_exchange(d, left_x, 84.0 * mm, left_w, 4.8 * mm)
    draw_engine(d, left_x, 43.1 * mm, left_w, 40.5 * mm)
    draw_hpc(d, left_x, 28.2 * mm, left_w, 13.2 * mm)
    draw_recovery(d, left_x, 6.5 * mm, left_w, 19.8 * mm)
    draw_terminal(d, terminal_x, 6.5 * mm, terminal_w, 98.0 * mm)

    # A single cross-layer connector keeps the main flow visually quiet.
    arrow(d, left_x + left_w / 2, 43.1 * mm, left_x + left_w / 2, 41.35 * mm, fill="teal", sw=0.9, head=1.0 * mm)
    return d


def main():
    PDF_OUT.parent.mkdir(parents=True, exist_ok=True)
    SVG_OUT.parent.mkdir(parents=True, exist_ok=True)
    drawing = build()
    renderPDF.drawToFile(drawing, str(PDF_OUT), "taskflow supervised workflow architecture")
    renderSVG.drawToFile(drawing, str(SVG_OUT))

    # Keep SVG text editable and portable outside the ReportLab font registry.
    svg = SVG_OUT.read_text(encoding="utf-8")
    replacements = {
        "font-family: TaskflowSans-Bold;": "font-family: Arial, Helvetica, sans-serif; font-weight: bold;",
        "font-family: TaskflowSans;": "font-family: Arial, Helvetica, sans-serif;",
        "font-family: TaskflowMono-Bold;": "font-family: Consolas, 'Courier New', monospace; font-weight: bold;",
        "font-family: TaskflowMono;": "font-family: Consolas, 'Courier New', monospace;",
    }
    for old, new in replacements.items():
        svg = svg.replace(old, new)
    SVG_OUT.write_text(svg, encoding="utf-8")
    print(PDF_OUT)
    print(SVG_OUT)
    print(f"Render {PNG_OUT.name} from the PDF at 600 dpi with pdftoppm.")


if __name__ == "__main__":
    main()
