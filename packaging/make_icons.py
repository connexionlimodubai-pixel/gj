"""Regenerate OpenBerry's desktop-app icons from the dashboard's brand mark.

Run from the repository root (Pillow and Playwright are build-time tools, not runtime dependencies):

    uv pip install --python .venv/bin/python pillow playwright
    .venv/bin/python packaging/make_icons.py            # render and rewrite packaging/icons/*
    .venv/bin/python packaging/make_icons.py --check    # verify the committed icons (no browser needed)

Rendering uses Playwright's Chromium (`playwright install chromium` if it is missing), or the browser
passed with `--chromium /path/to/chrome`. The output is byte-for-byte reproducible for a given Chromium
build and Pillow version.

The artwork is src/openberry/web/static/favicon.svg, used unchanged, so the app icon always matches the
dashboard's logo (a berry-coloured rounded square with the white berry). Change the favicon and rerun
this script to update every icon. Each size is rendered from the vector, not downscaled, with the mark
snapped to whole pixels so edges stay crisp:

- openberry-{1024,512,256}.png and openberry.icns: the macOS app-icon grid (the mark fills 824 of 1024
  pixels) with a soft drop shadow. Linux desktops and the app window use the PNGs.
- openberry.ico (Windows, 16-256 px): the mark nearly fills each frame, as Windows icons do, so the
  taskbar and title-bar sizes stay legible. No shadow.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # Pillow is only imported when rendering or checking.
    from PIL.Image import Image

ROOT = Path(__file__).resolve().parent.parent
MARK_SVG = ROOT / "src" / "openberry" / "web" / "static" / "favicon.svg"
ICONS_DIR = Path(__file__).resolve().parent / "icons"

PNG_SIZES = (1024, 512, 256)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICNS_SIZES = (32, 64, 128, 256, 512, 1024)  # every size Pillow's ICNS writer stores (ic07-ic14)

BRAND_RGB = (0xB0, 0x17, 0x5C)  # the favicon's background, the dashboard's --accent
BERRY_RGB = (0xFF, 0xFF, 0xFF)
# Points in the 32x32 mark used by --check: plain background, and the berry below its seeds.
BRAND_POINT = (4.0, 16.0)
BERRY_POINT = (16.0, 23.0)

_SVG_RE = re.compile(r"<svg\b(?P<attrs>[^>]*)>(?P<body>.*)</svg>\s*$", re.DOTALL)
_VIEWBOX_RE = re.compile(r'viewBox="(?P<box>[^"]+)"')


@dataclass(frozen=True)
class Mark:
    """The brand mark: the favicon's viewBox and its inner SVG elements."""

    view_box: str
    body: str


@dataclass(frozen=True)
class IconStyle:
    """How the mark sits on its canvas for one platform."""

    padding: float  # transparent margin on each side, as a fraction of the icon size
    shadow: bool
    full_bleed_upto: int = 0  # sizes up to this many pixels get no margin at all

    def margin(self, size: int) -> int:
        """The margin in whole pixels for a `size` x `size` icon, so the mark's edges land on pixels."""
        return 0 if size <= self.full_bleed_upto else round(size * self.padding)


MACOS = IconStyle(padding=100 / 1024, shadow=True)  # Apple's grid: an 824 px body on a 1024 px canvas
WINDOWS = IconStyle(padding=1 / 16, shadow=False, full_bleed_upto=32)  # taskbar sizes use every pixel


def read_mark(path: Path = MARK_SVG) -> Mark:
    """Split the favicon SVG into its viewBox and inner markup."""
    match = _SVG_RE.search(path.read_text(encoding="utf-8").strip())
    view_box = _VIEWBOX_RE.search(match.group("attrs")) if match else None
    if not match or not view_box:
        raise ValueError(f"{path} is not a single <svg> element with a viewBox")
    return Mark(view_box=view_box.group("box"), body=match.group("body"))


def icon_svg(mark: Mark, size: int, style: IconStyle) -> str:
    """A `size` x `size` SVG with the mark centred, padded and (for macOS) shadowed."""
    inset = style.margin(size)
    body = size - 2 * inset
    nested = (
        f'<svg x="{inset}" y="{inset}" width="{body}" height="{body}" viewBox="{mark.view_box}">'
        f"{mark.body}</svg>"
    )
    if not style.shadow:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 {size} {size}">{nested}</svg>'
        )
    dy, blur = size * 10 / 1024, size * 9 / 1024  # a soft shadow that stays inside the padding
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
        f'<defs><filter id="shadow" x="-25%" y="-25%" width="150%" height="150%">'
        f'<feDropShadow dx="0" dy="{dy:.3f}" stdDeviation="{blur:.3f}" flood-color="#000" flood-opacity="0.28"/>'
        f'</filter></defs><g filter="url(#shadow)">{nested}</g></svg>'
    )


def _page(svg: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "html,body{margin:0;padding:0;background:transparent}svg{display:block}"
        f"</style></head><body>{svg}</body></html>"
    )


def render(svgs: Sequence[tuple[str, int]], chromium: str | None = None) -> list[Image]:
    """Render each (svg, size) pair to a transparent RGBA image with headless Chromium."""
    from io import BytesIO

    from PIL import Image as PILImage
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    images: list[Image] = []
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                executable_path=chromium,
                args=["--force-color-profile=srgb", "--disable-lcd-text"],
            )
        except PlaywrightError as exc:
            reason = str(exc).splitlines()[0]
            raise RuntimeError(
                f"could not start Chromium ({reason}). Run `.venv/bin/playwright install chromium` "
                "or pass --chromium /path/to/chrome."
            ) from None
        try:
            page = browser.new_page(device_scale_factor=1)
            for svg, size in svgs:
                page.set_viewport_size({"width": size, "height": size})
                page.set_content(_page(svg))
                shot = page.screenshot(
                    omit_background=True, type="png", clip={"x": 0, "y": 0, "width": size, "height": size}
                )
                image = PILImage.open(BytesIO(shot)).convert("RGBA")
                if image.size != (size, size):
                    raise RuntimeError(f"Chromium rendered {image.size}, expected {size}x{size}")
                images.append(image)
        finally:
            browser.close()
    return images


def render_styles(
    mark: Mark, wanted: Sequence[tuple[IconStyle, Iterable[int]]], chromium: str | None = None
) -> list[dict[int, Image]]:
    """Render the mark in each (style, sizes) pair in one browser session: one {size: image} per pair."""
    plans = [(style, sorted(set(sizes))) for style, sizes in wanted]
    jobs = [(icon_svg(mark, size, style), size) for style, sizes in plans for size in sizes]
    images = iter(render(jobs, chromium))
    return [{size: next(images) for size in sizes} for _style, sizes in plans]


def write_icons(macos: dict[int, Image], windows: dict[int, Image], out_dir: Path = ICONS_DIR) -> list[Path]:
    """Write the PNGs, the .ico and the .icns into `out_dir` and return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for size in PNG_SIZES:
        path = out_dir / f"openberry-{size}.png"
        macos[size].save(path, "PNG", optimize=True)
        written.append(path)

    ico = out_dir / "openberry.ico"
    largest = max(ICO_SIZES)
    windows[largest].save(
        ico,
        "ICO",
        sizes=[(size, size) for size in ICO_SIZES],
        append_images=[windows[size] for size in ICO_SIZES if size != largest],
    )
    written.append(ico)

    icns = out_dir / "openberry.icns"
    largest = max(ICNS_SIZES)
    macos[largest].save(icns, "ICNS", append_images=[macos[size] for size in ICNS_SIZES if size != largest])
    written.append(icns)
    return written


def _close(rgb: tuple[int, ...], want: tuple[int, int, int], tolerance: int = 8) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(rgb, want, strict=True))


def check_image(image: Image, size: int, style: IconStyle, label: str, *, colours: bool = True) -> list[str]:
    """Problems with one rendered icon: wrong size or mode, opaque corners, or off-brand colours."""
    problems: list[str] = []
    if image.size != (size, size):
        return [f"{label}: size {image.size}, expected {size}x{size}"]
    rgba = image.convert("RGBA")
    if image.mode != "RGBA":
        problems.append(f"{label}: mode {image.mode}, expected RGBA")
    if rgba.getpixel((0, 0))[3] != 0:
        problems.append(f"{label}: top-left corner is not transparent")
    if not colours:
        return problems
    inset = style.margin(size)
    scale = (size - 2 * inset) / 32
    for (mx, my), want, what in ((BRAND_POINT, BRAND_RGB, "brand colour"), (BERRY_POINT, BERRY_RGB, "white berry")):
        pixel = rgba.getpixel((int(inset + mx * scale), int(inset + my * scale)))
        if pixel[3] != 255 or not _close(pixel[:3], want):
            problems.append(f"{label}: expected the {what} at mark ({mx}, {my}), got {pixel}")
    return problems


def check_icons(out_dir: Path = ICONS_DIR) -> list[str]:
    """Open every icon file in `out_dir` and return a list of problems (empty when all is well)."""
    from PIL import Image as PILImage

    problems: list[str] = []
    for size in PNG_SIZES:
        path = out_dir / f"openberry-{size}.png"
        if not path.is_file():
            problems.append(f"missing {path.name}")
            continue
        with PILImage.open(path) as image:
            if image.format != "PNG":
                problems.append(f"{path.name}: not a PNG")
            problems += check_image(image, size, MACOS, path.name)

    ico = out_dir / "openberry.ico"
    if not ico.is_file():
        problems.append("missing openberry.ico")
    else:
        with PILImage.open(ico) as image:
            sizes = sorted(image.info.get("sizes", ()))
            if sizes != [(size, size) for size in ICO_SIZES]:
                problems.append(f"openberry.ico: sizes {sizes}, expected {list(ICO_SIZES)}")
            for size in ICO_SIZES:
                if (size, size) in sizes:
                    frame = image.ico.getimage((size, size))
                    problems += check_image(frame, size, WINDOWS, f"openberry.ico@{size}", colours=size >= 32)

    icns = out_dir / "openberry.icns"
    if not icns.is_file():
        problems.append("missing openberry.icns")
    else:
        with PILImage.open(icns) as image:
            # Pillow lists ICNS entries as (points, points, scale): 1024 px is (512, 512, 2).
            entries = list(image.info.get("sizes", ()))
            missing = sorted(set(ICNS_SIZES) - {width * scale for width, _height, scale in entries})
            if image.format != "ICNS" or missing:
                problems.append(f"openberry.icns: format {image.format}, missing sizes {missing}")
            for width, height, scale in entries:
                pixels = width * scale
                frame = image.icns.getimage((width, height, scale))
                label = f"openberry.icns@{width}x{height}@{scale}x"
                problems += check_image(frame, pixels, MACOS, label, colours=pixels >= 64)
    return problems


def build(out_dir: Path = ICONS_DIR, chromium: str | None = None, mark_path: Path = MARK_SVG) -> list[Path]:
    """Render the mark and write every icon file."""
    macos, windows = render_styles(
        read_mark(mark_path), [(MACOS, (*PNG_SIZES, *ICNS_SIZES)), (WINDOWS, ICO_SIZES)], chromium
    )
    return write_icons(macos, windows, out_dir)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify the existing icons instead of rendering")
    parser.add_argument("--out", type=Path, default=ICONS_DIR, help=f"output folder (default: {ICONS_DIR})")
    parser.add_argument("--chromium", help="Chromium/Chrome executable (default: Playwright's own)")
    args = parser.parse_args(argv)

    if not args.check:
        try:
            written = build(args.out, args.chromium)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for path in written:
            shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            print(f"wrote {shown} ({path.stat().st_size:,} bytes)")
    problems = check_icons(args.out)
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    if not problems:
        print(f"icons OK in {args.out}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
