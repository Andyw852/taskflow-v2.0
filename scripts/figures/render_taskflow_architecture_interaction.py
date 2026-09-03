#!/usr/bin/env python3
"""Render taskflow architecture with a compact, state-driven control loop.

This independent variant preserves the approved architecture on the left and
uses a publication-friendly interaction panel on the right.
"""

from pathlib import Path

from reportlab.graphics import renderPDF, renderSVG
from reportlab.graphics.shapes import Circle, Drawing, Line, Rect
from reportlab.lib.colors import HexColor
from reportlab.lib.units import mm

import render_taskflow_architecture as base


# Increase all typography in this publication variant without changing geometry.
base.TEXT_SCALE = 1.10

# Restrained editorial palette: warm neutrals provide the hierarchy, while
# muted blue/teal/ochre/vermilion accents encode categories and states.
base.COLORS.update(
    {
        "ink": HexColor("#24313A"),
        "muted": HexColor("#66727C"),
        "faint": HexColor("#929DA6"),
        "line": HexColor("#D5DCE0"),
        "line_dark": HexColor("#A7B2BA"),
        "paper": HexColor("#FCFCFA"),
        "surface": HexColor("#F4F6F5"),
        "lavender": HexColor("#F1EFF4"),
        "navy": HexColor("#344A5A"),
        "navy_deep": HexColor("#263A47"),
        "blue": HexColor("#4C78A8"),
        "blue_soft": HexColor("#EAF0F5"),
        "purple": HexColor("#7A6F9B"),
        "teal": HexColor("#4C918F"),
        "teal_soft": HexColor("#E8F1EF"),
        "gold": HexColor("#C7943D"),
        "gold_soft": HexColor("#F7F0E3"),
        "coral": HexColor("#C66355"),
        "coral_soft": HexColor("#F6ECE9"),
        "green": HexColor("#5B8C76"),
        "terminal_text": HexColor("#EDF2F3"),
        "terminal_muted": HexColor("#AEC0C5"),
        "terminal_gold": HexColor("#D7B36A"),
        "terminal_green": HexColor("#95C6B8"),
    }
)


ROOT = Path(__file__).resolve().parents[2]
PDF_OUT = ROOT / "output" / "pdf" / "taskflow_architecture_interaction.pdf"
SVG_OUT = ROOT / "output" / "figures" / "taskflow_architecture_interaction.svg"
PNG_OUT = ROOT / "output" / "figures" / "taskflow_architecture_interaction.png"


def code_block(d, x, y, w, h, commands, prefix="$"):
    d.add(base.rounded(x, y, w, h, 1.0 * mm, fill="navy_deep", stroke="navy_deep", sw=0.4))
    if len(commands) == 1:
        baselines = [y + 2.0 * mm]
    else:
        baselines = [y + 3.2 * mm, y + 1.25 * mm]
    for baseline, command in zip(baselines, commands):
        base.text(d, x + 1.5 * mm, baseline, prefix, size=3.15, fill="terminal_green", bold=True, mono=True)
        base.text(d, x + 3.9 * mm, baseline, command, size=3.15, fill="terminal_text", mono=True)


def interaction_card(d, x, y, w, h, number, title_value, purpose, commands, accent, code_prefix="$"):
    d.add(base.rounded(x, y, w, h, 1.7 * mm, fill="paper", stroke="line", sw=0.6))
    d.add(Rect(x, y, 1.05 * mm, h, fillColor=base.color(accent), strokeColor=None))

    node_x = x - 3.3 * mm
    node_y = y + h / 2
    d.add(Circle(node_x, node_y, 1.42 * mm, fillColor=base.color(accent), strokeColor=base.color(accent)))
    base.text(d, node_x, node_y - 0.75 * mm, str(number), size=3.65, fill="paper", bold=True, anchor="middle")

    base.text(d, x + 3.0 * mm, y + h - 2.85 * mm, title_value, size=4.6, fill="ink", bold=True)
    base.text(d, x + 3.0 * mm, y + h - 5.3 * mm, purpose, size=3.45, fill="muted")
    code_block(d, x + 2.35 * mm, y + 0.7 * mm, w - 4.35 * mm, 4.55 * mm, commands, prefix=code_prefix)


def response_band(d, x, y, w, h, states):
    """Draw one compact state-to-action band instead of three large cards."""
    d.add(base.rounded(x, y, w, h, 1.45 * mm, fill="paper", stroke="line", sw=0.65))
    cell_w = w / len(states)
    for i, (state, action, accent) in enumerate(states):
        cell_x = x + i * cell_w
        if i:
            d.add(Line(cell_x, y + 1.25 * mm, cell_x, y + h - 1.25 * mm, strokeColor=base.color("line"), strokeWidth=0.55))
        d.add(Circle(cell_x + 2.0 * mm, y + h - 2.55 * mm, 0.82 * mm, fillColor=base.color(accent), strokeColor=None))
        base.text(d, cell_x + 3.4 * mm, y + h - 3.25 * mm, state, size=3.45, fill=accent, bold=True)
        base.text(d, cell_x + cell_w / 2, y + 1.8 * mm, action, size=2.95, fill="muted", bold=True, anchor="middle")


def draw_interaction_flow(d, x, y, w, h):
    d.add(base.rounded(x, y, w, h, 2.5 * mm, fill="surface", stroke="line", sw=0.7))
    d.add(Rect(x, y + h - 1.25 * mm, w, 1.25 * mm, fillColor=base.color("navy"), strokeColor=None))
    base.text(d, x + 4.0 * mm, y + h - 5.1 * mm, "Typical tf control loop", size=5.75, fill="ink", bold=True)
    base.text(
        d,
        x + 4.0 * mm,
        y + h - 8.7 * mm,
        "LLM-planned, state-driven execution",
        size=3.65,
        fill="muted",
    )

    card_x = x + 6.7 * mm
    card_w = w - 9.3 * mm
    card_h = 12.9 * mm
    card_y = [y + 72.5 * mm, y + 57.0 * mm, y + 41.5 * mm, y + 26.0 * mm]
    node_x = card_x - 3.3 * mm
    centers = [yy + card_h / 2 for yy in card_y]

    # Short arrows make the four-layer direction explicit without crossing nodes.
    for upper, lower in zip(centers, centers[1:]):
        base.arrow(
            d,
            node_x,
            upper - 1.55 * mm,
            node_x,
            lower + 1.7 * mm,
            fill="line_dark",
            sw=0.8,
            head=0.75 * mm,
        )

    cards = [
        (
            1,
            "LLM supervisor",
            "goal + policy + feedback -> action plan",
            ["context: goal + policy + state", "plan: proposed tf action"],
            "purple",
            ">",
        ),
        (2, "Configure", "initialize -> skill -> HPC (optional)", ["tf -p MAT init", "tf skills"], "gold", "$"),
        (3, "Start / Advance", "generate inputs -> submit -> advance", ["tf -p MAT start", "tf start"], "teal", "$"),
        (4, "Monitor / Inspect", "observe changes -> inspect -> report", ["tf summary --diff", "tf -p MAT status"], "blue", "$"),
    ]
    for yy, (number, title_value, purpose, commands, accent, code_prefix) in zip(card_y, cards):
        interaction_card(d, card_x, yy, card_w, card_h, number, title_value, purpose, commands, accent, code_prefix)

    # Monitoring yields state-driven responses, not a mandatory fourth stage.
    response_y = y + 3.0 * mm
    response_h = 8.2 * mm
    response_cell_w = card_w / 3
    branch_x = card_x + card_w / 2
    branch_y = y + 18.0 * mm
    monitor_bottom = card_y[-1]
    response_label_y = y + 19.2 * mm
    base.text(d, card_x, response_label_y, "Observed state -> response", size=3.45, fill="muted", bold=True)
    d.add(Line(branch_x, monitor_bottom, branch_x, branch_y, strokeColor=base.color("line_dark"), strokeWidth=0.8))
    state_centers = [card_x + (i + 0.5) * response_cell_w for i in range(3)]
    d.add(Line(state_centers[0], branch_y, state_centers[-1], branch_y, strokeColor=base.color("line_dark"), strokeWidth=0.8))
    for state_x, accent in zip(state_centers, ["blue", "green", "coral"]):
        d.add(Line(state_x, branch_y, state_x, response_y + response_h, strokeColor=base.color(accent), strokeWidth=0.85))
    response_band(
        d,
        card_x,
        response_y,
        card_w,
        response_h,
        [
            ("RUN / PD", "monitor", "blue"),
            ("OK", "advance / finish", "green"),
            ("FAIL", "diagnose / retry", "coral"),
        ],
    )

    # All observed states return to the LLM as compact state and exit-code feedback.
    feedback_x = x + w - 1.2 * mm
    response_center_y = response_y + response_h / 2
    llm_center_y = centers[0]
    d.add(Line(card_x + card_w, response_center_y, feedback_x, response_center_y, strokeColor=base.color("teal"), strokeWidth=0.85))
    d.add(Line(feedback_x, response_center_y, feedback_x, llm_center_y, strokeColor=base.color("teal"), strokeWidth=0.85))
    base.arrow(d, feedback_x, llm_center_y, card_x + card_w + 0.1 * mm, llm_center_y, fill="teal", sw=0.85, head=0.8 * mm)
    base.text(d, feedback_x - 0.8 * mm, response_label_y, "FEEDBACK", size=3.55, fill="teal", bold=True, anchor="end")


def build():
    d = Drawing(base.W, base.H)
    d.add(Rect(0, 0, base.W, base.H, fillColor=base.color("paper"), strokeColor=None))

    base.text(
        d,
        7.0 * mm,
        112.7 * mm,
        "taskflow: LLM-guided orchestration of materials workflows",
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
    panel_x, panel_w = 122.0 * mm, 51.0 * mm

    base.draw_supervision(d, left_x, 89.3 * mm, left_w, 15.2 * mm)
    base.draw_control_exchange(d, left_x, 84.0 * mm, left_w, 4.8 * mm)
    base.draw_engine(d, left_x, 43.1 * mm, left_w, 40.5 * mm)
    base.draw_hpc(d, left_x, 28.2 * mm, left_w, 13.2 * mm)
    base.draw_recovery(d, left_x, 6.5 * mm, left_w, 19.8 * mm)
    draw_interaction_flow(d, panel_x, 6.5 * mm, panel_w, 98.0 * mm)

    base.arrow(d, left_x + left_w / 2, 43.1 * mm, left_x + left_w / 2, 41.35 * mm, fill="teal", sw=0.9, head=1.0 * mm)
    return d


def main():
    PDF_OUT.parent.mkdir(parents=True, exist_ok=True)
    SVG_OUT.parent.mkdir(parents=True, exist_ok=True)
    drawing = build()
    renderPDF.drawToFile(drawing, str(PDF_OUT), "taskflow architecture - light interaction flow")
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
