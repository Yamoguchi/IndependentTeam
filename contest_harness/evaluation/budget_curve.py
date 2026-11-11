"""Score-versus-budget artifacts for independently checked benchmark series."""

from html import escape

from contest_harness.evaluation.artifacts import read_jsonl, write_text

COLORS = {"F": "#2364aa", "G": "#bf4e18", "E1": "#388354", "A": "#388354"}


def retrospective(run, caps):
    tasks, _ = read_jsonl(run / "results-by-task.jsonl")
    known = [(task["usage"]["exact_tenths"], task["score"]) for task in tasks
             if task["passed"] and task["score"] > 0 and task["usage"]["exact_tenths"] is not None]
    best_cost = {0: 0}
    for cost, reward in known:
        for score, spend in list(best_cost.items()):
            total = spend + cost
            if total <= max(caps):
                best_cost[score + reward] = min(total, best_cost.get(score + reward, total))
    return [max(score for score, cost in best_cost.items() if cost <= cap) for cap in caps], len(known)


def chart(path, series):
    modes = max(len({row["mode"] for row in points}) for points in series.values())
    width, height = 1040, 344 + 16 * ((modes - 1) // 7)
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Score vs Budget, локальные баллы">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<g font-family="sans-serif" font-size="13" fill="#20252b">',
        '<text x="520" y="25" text-anchor="middle" font-size="17">Score vs Budget</text>',
    ]
    for index, (label, points) in enumerate(series.items()):
        x, y, w, h = 68 + index * 510, 57, 396, 216
        xmax = max(row["limit_tenths"] for row in points) / 10
        ymax = max(1, max(row["max_score"] for row in points))

        def xy(spend, score):
            return x + w * spend / xmax, y + h * (1 - score / ymax)

        elements.extend([
            f'<text x="{x+w/2}" y="{y-10}" text-anchor="middle">{escape(label)}</text>',
            f'<text transform="translate({x-44} {y+h/2}) rotate(-90)" text-anchor="middle">Локальные баллы</text>',
        ])
        for pct in (0, 25, 50, 75, 100):
            xx, yy = xy(xmax * pct / 100, ymax * pct / 100)
            elements.extend([
                f'<path d="M{x} {yy}h{w} M{xx} {y}v{h}" stroke="#e0e4e8"/>',
                f'<text x="{x-9}" y="{yy+4}" text-anchor="end">{ymax*pct/100:g}</text>',
                f'<text x="{xx}" y="{y+h+18}" text-anchor="middle">{xmax*pct/100:,.0f}</text>',
            ])
        elements.append(f'<text x="{x+w/2}" y="{y+h+40}" text-anchor="middle">Фактический расход, вых. экв.</text>')
        for n, mode in enumerate(dict.fromkeys(row["mode"] for row in points)):
            color = COLORS.get(mode, "#60558b")
            coords = [xy(row["output_equivalent"], row["score"]) for row in sorted(points, key=lambda r: r["limit_tenths"])
                      if row["mode"] == mode and row["output_equivalent"] is not None]
            elements.append(f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a,b in coords)}" '
                            f'fill="none" stroke="{color}" stroke-width="2"/>')
            for a, b in coords:
                elements.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="{4+n%3}" fill="none" stroke="{color}"/>')
            elements.append(f'<text x="{x+(n%7)*55}" y="{y+h+61+(n//7)*16}" fill="{color}">{escape(mode)}</text>')
    elements.append('</g></svg>')
    write_text(path, "\n".join(elements) + "\n")
