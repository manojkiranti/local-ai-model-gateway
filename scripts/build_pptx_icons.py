"""Render create_pptx's icon set (Lucide, ISC) to the committed PNG assets.

DEV-TIME ONLY. The output lives in `app/tools/local/assets/icons/` and is
committed; nothing here runs in the API or worker. Re-run only when
`app/tools/local/pptx_icons.ICON_NAMES` changes.

Icon geometry is read from the frontend's own `lucide-react` package, so the
deck's icons and the in-app preview's are the same drawings. Needs `node` and
`cairosvg` (with the native cairo library), neither of which is a project
dependency — use a throwaway venv:

    python3 -m venv /tmp/iconvenv && /tmp/iconvenv/bin/pip install cairosvg
    DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib \\
      /tmp/iconvenv/bin/python scripts/build_pptx_icons.py \\
      --lucide ../local-ai-model-frontend/node_modules/lucide-react
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Loaded by FILE, not via `app.tools.local`: that package's __init__ imports
# every tool and their dependencies, which this dev-only venv deliberately lacks.
_spec = importlib.util.spec_from_file_location(
    "pptx_icons", REPO / "app" / "tools" / "local" / "pptx_icons.py"
)
_icons = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_icons)
ICON_DIR, ICON_NAMES = _icons.ICON_DIR, _icons.ICON_NAMES

# Brand red (#E60012, create_pptx's _STAT_VALUE_COLOR) for icons on a pale
# circle; white for icons on a brand-coloured band (comparison headers).
VARIANTS = {"red": "#E60012", "white": "#FFFFFF"}
SIZE_PX = 192  # ~1 inch at 192 dpi: crisp at the sizes the layouts draw

_NODE = r"""
const [dir, names] = [process.argv[1], JSON.parse(process.argv[2])];
(async () => {
  const out = {};
  for (const n of names) {
    const mod = await import(`${dir}/dist/esm/icons/${n}.mjs`);
    out[n] = mod.__iconNode;
  }
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _svg(nodes: list, color: str) -> str:
    parts = []
    for tag, attrs in nodes:
        rendered = " ".join(f'{k}="{v}"' for k, v in attrs.items() if k != "key")
        parts.append(f"<{tag} {rendered}/>")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round">{"".join(parts)}</svg>'
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lucide", required=True, type=Path, help="path to node_modules/lucide-react")
    args = parser.parse_args()

    import cairosvg

    lucide = args.lucide.resolve()
    raw = subprocess.run(
        ["node", "-e", _NODE, str(lucide), json.dumps(list(ICON_NAMES))],
        check=True, capture_output=True, text=True,
    ).stdout
    nodes = json.loads(raw)

    for variant, color in VARIANTS.items():
        out_dir = ICON_DIR / variant
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        for name in ICON_NAMES:
            cairosvg.svg2png(
                bytestring=_svg(nodes[name], color).encode(),
                write_to=str(out_dir / f"{name}.png"),
                output_width=SIZE_PX,
                output_height=SIZE_PX,
            )
    shutil.copy(lucide / "LICENSE", ICON_DIR / "LICENSE")
    print(f"rendered {len(ICON_NAMES)} icons x {len(VARIANTS)} variants into {ICON_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
