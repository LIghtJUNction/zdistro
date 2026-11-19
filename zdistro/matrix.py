"""README matrices distinguish evidence from installed tools and recipes."""

from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path

from .backends import ALL_HOSTS, BACKENDS, backend_for, doctor
from .model import DistroError, digest, host_name, inside


LABELS = {"verified-native": "Native verified", "verified-structure": "Integrity verified",
          "verified-recipe": "Recipe verified", "ready": "Tools found", "needs-setup": "Needs setup",
          "native-tools": "Native tools needed", "unsupported-host": "—", "receipt-only": "Receipt only"}
COLORS = {"verified-native": ("#e6f4ea", "#137333"), "verified-structure": ("#e8f0fe", "#1967d2"),
          "verified-recipe": ("#f3e8fd", "#8430ce"), "ready": ("#e8f0fe", "#1967d2"),
          "needs-setup": ("#fef7e0", "#8a5b00"), "native-tools": ("#f1f3f4", "#5f6368"),
          "unsupported-host": ("#ffffff", "#9aa0a6"), "receipt-only": ("#f1f3f4", "#5f6368")}


def matrix(manifest, targets, receipt_root):
    receipt_root = Path(receipt_root)
    evidence, notes = {}, []
    if receipt_root.exists():
        for path in receipt_root.rglob("receipt.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if (data.get("schema") != 1 or data.get("status") != "verified"
                        or data.get("manifest_sha256") != manifest.sha256
                        or data.get("target") not in targets or data.get("host") not in ALL_HOSTS):
                    continue
                key = (data["target"], data["host"])
                if key not in evidence or data["created_at"] > evidence[key][0]["created_at"]:
                    evidence[key] = (data, path)
            except (OSError, ValueError, KeyError, TypeError) as error:
                notes.append(f"Unreadable receipt {path.name}: {error}")
    rows = []
    for target in targets:
        backend = backend_for(manifest, target)
        local = doctor(manifest, target, probe=False)
        cells = {}
        for host in ALL_HOSTS:
            state = "unsupported-host" if host not in backend.hosts else "native-tools"
            cell = {"status": state, "label": LABELS[state]}
            if host in backend.hosts and host == host_name():
                state = "ready" if local["ready"] else "needs-setup"
                cell = {"status": state, "label": LABELS[state], "issues": local["issues"]}
            if (target, host) in evidence:
                data, path = evidence[(target, host)]
                try:
                    artifact = inside(path.parent, data["artifact"], "receipt artifact", False)
                    if artifact.is_file():
                        if digest(artifact) != data["sha256"] or artifact.stat().st_size != data["bytes"]:
                            raise DistroError("artifact checksum/size changed")
                        state = "verified-" + data["verification"]["level"]
                        if state not in LABELS:
                            raise DistroError("unknown verification level")
                    else:
                        state = "receipt-only"
                    cell = {"status": state, "label": LABELS[state], "sha256": data["sha256"],
                            "checks": data["verification"]["checks"], "created_at": data["created_at"]}
                except (OSError, ValueError, KeyError, TypeError, DistroError) as error:
                    notes.append(f"{target}/{host}: invalid evidence: {error}")
            cells[host] = cell
        builder = {"crate": "Cargo", "wheel": "PEP 517", "sdist": "PEP 517", "npm": "npm",
                   "apk": "AAPT2 / Gradle", "aab": "Gradle", "docker": "Docker / Podman"}.get(
                       target, manifest.data.get("build", {}).get("kind", "none"))
        rows.append({"target": target, "builder": "staged files" if builder == "none" else builder,
                     "description": backend.description, "platforms": cells})
    return {"schema": 1, "package": manifest.package["name"], "version": manifest.package["version"],
            "manifest_sha256": manifest.sha256, "generated_at": datetime.now(timezone.utc).isoformat(),
            "host": host_name(), "rows": rows, "notes": notes,
            "scope": "Build artifact verification; not installation, signatures, store approval or arbitrary software compatibility."}


def render(data, format):
    if format == "json":
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if format == "md":
        lines = [f"## {data['package']} {data['version']} packaging matrix", "",
                 "| Format / builder | Linux | macOS | Windows |", "|---|---|---|---|"]
        for row in data["rows"]:
            cells = [row["platforms"][host]["label"] for host in ALL_HOSTS]
            lines.append(f"| {row['target']} / {row['builder']} | " + " | ".join(cells) + " |")
        lines.extend(["", data["scope"], "", "Tools found is a prerequisite check, not a tested build.", ""])
        return "\n".join(lines)
    width, row_height = 900, 48
    height = 186 + row_height * len(data["rows"])
    e = lambda value: escape(str(value), quote=True)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
             '<title id="title">Packaging matrix</title>', f'<desc id="desc">{e(data["scope"])}</desc>',
             f'<rect width="{width}" height="{height}" rx="16" fill="#fff" stroke="#dadce0"/>',
             '<g font-family="Arial,Helvetica,sans-serif">',
             '<text x="28" y="38" font-size="22" font-weight="600" fill="#202124">Packaging matrix</text>',
             f'<text x="28" y="62" font-size="12" fill="#5f6368">{e(data["package"])} {e(data["version"])} · manifest {e(data["manifest_sha256"][:12])}</text>']
    columns = [372, 558, 744]
    for x, title in zip(columns, ("Linux", "macOS", "Windows")):
        parts.append(f'<text x="{x}" y="98" text-anchor="middle" font-size="13" font-weight="600" fill="#3c4043">{title}</text>')
    parts.append('<line x1="28" x2="872" y1="111" y2="111" stroke="#e8eaed"/>')
    for index, row in enumerate(data["rows"]):
        y = 112 + index * row_height
        parts.extend([f'<text x="28" y="{y+21}" font-size="14" font-weight="600" fill="#202124">{e(row["target"])}</text>',
                      f'<text x="28" y="{y+37}" font-size="11" fill="#5f6368">{e(row["builder"])}</text>'])
        for x, host in zip(columns, ALL_HOSTS):
            cell = row["platforms"][host]
            background, foreground = COLORS[cell["status"]]
            parts.append(f'<rect x="{x-83}" y="{y+10}" width="166" height="27" rx="7" fill="{background}"/>')
            parts.append(f'<text x="{x}" y="{y+28}" text-anchor="middle" font-size="11" fill="{foreground}">{e(cell["label"])}</text>')
        parts.append(f'<line x1="28" x2="872" y1="{y+48}" y2="{y+48}" stroke="#f1f3f4"/>')
    y = 139 + row_height * len(data["rows"])
    parts.extend([f'<text x="28" y="{y}" font-size="11" fill="#5f6368">Verified = artifact evidence. Tools found = prerequisites only. — = no built-in backend on this host.</text>',
                  f'<text x="28" y="{y+20}" font-size="11" fill="#5f6368">Installation, signing, store approval and app behavior require their own checks.</text>', '</g></svg>\n'])
    return "\n".join(parts)
