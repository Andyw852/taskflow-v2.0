#!/usr/bin/env python3
"""Render an alternative taskflow architecture with an association connector.

This variant preserves the approved figure and replaces only the gold downward
arrow inside the orchestration engine. A thin line, two nodes, and a short cap
on the skill-pool boundary express structural association without implying a
one-way execution step.
"""

from pathlib import Path

from reportlab.graphics import renderPDF, renderSVG
from reportlab.graphics.shapes import Circle, Drawing, Line, Rect
from reportlab.lib.units import mm

import render_taskflow_architecture as base


ROOT = Path(__file__).resolve().parents[2]
PDF_OUT = ROOT / "output" / "pdf" / "taskflow_architecture_association.pdf"
SVG_OUT = ROOT / "output" / "figures" / "taskflow_architecture_association.svg"
PNG_OUT = ROOT / "output" / "figures" / "taskflow_architecture_association.png"


def draw_skill_association(d, cx, card_bottom, pool_top):
    """Connect the skill card to its module pool without an arrowhead."""
    gold = base.color("gold")
    d.add(Line(cx, card_bottom - 0.35 * mm, cx, pool_top + 0.25 * mm, strokeColor=gold, strokeWidth=1.0))
    d.add(Circle(cx, card_bottom - 0.35 * mm, 0.58 * mm, fillColor=gold, strokeColor=gold))
    d.add(Line(cx - 5.2 * mm, pool_top, cx + 5.2 * mm, pool_top, strokeColor=gold, strokeWidth=1.15))
    d.add(Circle(cx, pool_top + 0.25 * mm, 0.72 * mm, fillColor=base.color("paper"), strokeColor=gold, strokeWidth=0.8))


def draw_engine(d, x, y, w, h):
    base.section_shell(d, x, y, w, h, "taskflow orchestration engine", "navy", fill="surface")

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
        base.info_card(d, xx, row_y, row_w, row_h, title_value, subtitle, accent)
        if i < 2:
            base.arrow(
                d,
                xx + row_w + 0.35 * mm,
                row_y + row_h / 2,
                xx + row_w + gap - 0.35 * mm,
                row_y + row_h / 2,
                fill="navy",
                sw=0.8,
                head=1.0 * mm,
            )

    cat_x = x + 3.0 * mm
    cat_y = y + 2.7 * mm
    cat_w = w - 6.0 * mm
    cat_h = 18.2 * mm
    d.add(base.rounded(cat_x, cat_y, cat_w, cat_h, 1.7 * mm, fill="paper", stroke="line", sw=0.6))
    base.text(d, cat_x + 2.5 * mm, cat_y + cat_h - 3.8 * mm, "Extensible skill pool", size=4.55, fill="ink", bold=True)
    base.text(
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
    start_x = cat_x + label_w + 2.0 * mm

    dft_y = cat_y + 7.2 * mm
    dft_h = 5.5 * mm
    base.text(d, cat_x + 2.5 * mm, dft_y + 1.7 * mm, "DFT", size=3.7, fill="blue", bold=True)
    dft = [("opt-dft", "relax"), ("band-dft", "bands"), ("ke-dft", "e-thermal"), ("kl-dft", "lattice k")]
    dft_w = (usable_w - 4 * box_gap) / 5
    for i, (title_value, subtitle) in enumerate(dft):
        base.skill_box(d, start_x + i * (dft_w + box_gap), dft_y, dft_w, dft_h, title_value, subtitle, "blue", "blue_soft")
    base.ellipsis_box(d, start_x + 4 * (dft_w + box_gap), dft_y, dft_w, dft_h, "blue")

    mace_y = cat_y + 1.2 * mm
    mace_h = 5.3 * mm
    base.text(d, cat_x + 2.5 * mm, mace_y + 1.6 * mm, "MACE", size=3.7, fill="teal", bold=True)
    mace = [
        ("opt-mace", "CPU / GPU"),
        ("kl-mace", "CPU / GPU"),
        ("phonon-mace", "dispersion"),
        ("mlff-mace", "MLFF training"),
    ]
    mace_w = (usable_w - 4 * box_gap) / 5
    for i, (title_value, subtitle) in enumerate(mace):
        base.skill_box(d, start_x + i * (mace_w + box_gap), mace_y, mace_w, mace_h, title_value, subtitle, "teal", "teal_soft")
    base.ellipsis_box(d, start_x + 4 * (mace_w + box_gap), mace_y, mace_w, mace_h, "teal")

    cx = row_x + row_w * 1.5 + gap
    draw_skill_association(d, cx, row_y, cat_y + cat_h)


def build():
    d = Drawing(base.W, base.H)
    d.add(Rect(0, 0, base.W, base.H, fillColor=base.color("paper"), strokeColor=None))

    base.text(
        d,
        7.0 * mm,
        112.7 * mm,
        "taskflow: an LLM-supervised VASP/MACE workflow manager for heterogeneous HPC",
        size=9.15,
        fill="ink",
        bold=True,
    )
    base.text(
        d,
        7.0 * mm,
        108.5 * mm,
        "Human authorization and explicit policy govern a reproducible, state-aware execution engine.",
        size=4.45,
        fill="muted",
    )

    left_x, left_w = 7.0 * mm, 111.0 * mm
    terminal_x, terminal_w = 122.0 * mm, 51.0 * mm

    base.draw_supervision(d, left_x, 89.3 * mm, left_w, 15.2 * mm)
    base.draw_control_exchange(d, left_x, 84.0 * mm, left_w, 4.8 * mm)
    draw_engine(d, left_x, 43.1 * mm, left_w, 40.5 * mm)
    base.draw_hpc(d, left_x, 28.2 * mm, left_w, 13.2 * mm)
    base.draw_recovery(d, left_x, 6.5 * mm, left_w, 19.8 * mm)
    base.draw_terminal(d, terminal_x, 6.5 * mm, terminal_w, 98.0 * mm)

    base.arrow(d, left_x + left_w / 2, 43.1 * mm, left_x + left_w / 2, 41.35 * mm, fill="teal", sw=0.9, head=1.0 * mm)
    return d


def main():
    PDF_OUT.parent.mkdir(parents=True, exist_ok=True)
    SVG_OUT.parent.mkdir(parents=True, exist_ok=True)
    drawing = build()
    renderPDF.drawToFile(drawing, str(PDF_OUT), "taskflow architecture - association connector variant")
    renderSVG.drawToFile(drawing, str(SVG_OUT))

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
