import json

from meizel.source_evidence.model import TsReport


def render_ts_report(report: TsReport) -> list[str]:
    if report.status == "not-applicable":
        return ["TS source evidence: not-applicable (no selected runtime TS sources)."]
    if report.status == "not-requested":
        return ["TS source evidence: not-requested (no supplied index; no Node process started)."]
    lines = [
        f"TS source evidence: {report.status} (non-gating; no runtime execution or semantic purity proof).",
        "  independent AST source snapshot; graph/diagnostic snapshot identity is not established.",
        "  module-owned allocation capability is distinct from actual sharing, writes or effects;",
        "  const and shallow freeze do not establish collection immutability; unknown is not safe.",
    ]
    for source in report.sources:
        lines.append(f"  {source.path} sha256={source.sha256} [{source.status}]: {source.reason}")
        if not source.observations:
            lines.append("    no supported observations; unanalysed behavior remains unknown.")
        for item in source.observations:
            binding = (
                item.binding + "".join(f"[{json.dumps(p)}]" for p in item.properties)
            ) or "<unsupported region>"
            wrapper = "; shallow frozen surface only" if item.frozen_surface else ""
            lines.append(
                f"    {source.path}:{item.line}-{item.end_line} {binding}: "
                f"{item.kind} {item.constructor}{wrapper}; {item.reason}"
            )
            lines.extend(f"      declaration: {d.dependency}:{d.line}" for d in item.declarations)
    return lines
