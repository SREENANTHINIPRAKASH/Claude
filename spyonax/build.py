"""Build the SPYONAX sector-concepts page and its Markdown copy from concepts.json.

Usage: python3 spyonax/build.py
Writes spyonax/sector-concepts.html and spyonax/README.md.
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
data = json.loads((HERE / "concepts.json").read_text(encoding="utf-8"))

# Page: inject the data into the template's script.
payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
template = (HERE / "template.html").read_text(encoding="utf-8")
assert "/*DATA*/null" in template
(HERE / "sector-concepts.html").write_text(template.replace("/*DATA*/null", payload), encoding="utf-8")

# Markdown copy.
sectors = data["sectors"]
by_order = sorted(sectors, key=lambda s: s["order"])
total = lambda s: sum(s["scores"].values())
crit = data["criteria"]
bp = data["blueprint"]

md = [
    "# SPYONAX Sector Concepts",
    "",
    f"*From Earth to Markets to Space.* Companion to the [{bp['title']}]({bp['url']}) ({bp['draft']}). Updated {data['updated']}.",
    "",
    "The strongest business concept for each of the ten sectors, how they connect, and the order to build them in.",
    "",
    f"> **The one idea.** {data['thesis']}",
    "",
    "## From Earth to Markets to Space",
    "",
]
for i, arc in enumerate(data["arcs"]):
    names = ", ".join(s["short"] for s in sorted(sectors, key=lambda s: s["order"]) if s["arc"] == arc["id"])
    md.append(f"- **{arc['name']}** ({arc['note'].lower()}): {names}")
    if i < len(data["arcs"]) - 1:
        md.append(f"  - ↓ {arc['flow']}")
md += [f"- ↺ **Space back to Earth:** {data['arcs'][-1]['flow']}.", ""]

md += ["## Build order", "", "| # | Sector | Best concept | First money from | Starts |", "|---|---|---|---|---|"]
for s in by_order:
    md.append(f"| {s['order']} | {s['short']} | {s['headline']} | {s['firstMoney']} | {s['start']} |")
md += ["", f"**If you build only three.** {data['three'].replace('If you build only three, build ', 'Build ')}", ""]

md += ["## Scorecard", "", "My scoring from 1 to 5 (5 is best) for each sector's entry concept.", ""]
md.append("| Sector | " + " | ".join(c[1] for c in crit) + " | Total /25 |")
md.append("|---|" + "---:|" * (len(crit) + 1))
for s in sorted(sectors, key=lambda s: (-total(s), s["order"])):
    md.append(f"| {s['short']} | " + " | ".join(str(s["scores"][c[0]]) for c in crit) + f" | **{total(s)}** |")
md += [""] + [f"- **{c[1]}:** {c[2]}" for c in crit] + [""]

md += ["## The ten concepts", ""]
for arc in data["arcs"]:
    md += [f"### {arc['name']}", ""]
    for s in [s for s in by_order if s["arc"] == arc["id"]]:
        md += [
            f"#### {s['name']} · {data['pillars'][s['pillar']]} pillar · #{s['order']} to build · {s['start']}",
            "",
            f"**{s['concept']}**",
            "",
            f"- **Flagship:** {', '.join(s['flagship'])}",
            f"- **Customers:** {s['customers']}",
            f"- **Money:** {s['money']}",
            f"- **Group edge:** {s['edge']}",
            "- **First 90 days:**",
            *[f"  {n}. {t}" for n, t in enumerate(s["first90"], 1)],
            f"- **Gate to scale:** {s['gate']}",
            f"- **Key number:** {s['kpi']}",
            f"- **Biggest risk:** {s['risk']}",
            f"- **Check with counsel:** {s['check']}",
            f"- **Feeds:** {', '.join(s['feeds'])}",
            "",
        ]

md += ["## Changes I'd make to the Blueprint", "", "Each of these is your call. None changes a name or a naming rule.", ""]
md += [f"{n}. **{t}** {d}" for n, (t, d) in enumerate(data["changes"], 1)] + [""]
md += ["## Next 30 days", ""]
md += [f"{n}. **{w}:** {t}" for n, (w, t) in enumerate(data["next30"], 1)] + [""]
md += ["---", ""] + [f"- {n}" for n in data["notes"]] + [""]

(HERE / "README.md").write_text("\n".join(md), encoding="utf-8")
print("wrote sector-concepts.html and README.md")
