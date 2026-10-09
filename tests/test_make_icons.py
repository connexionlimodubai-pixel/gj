"""packaging/make_icons.py: the desktop-app icons rendered from the dashboard's favicon.

Pillow and Playwright are build-time tools, not dependencies: the tests that need them skip without them.
"""

from __future__ import annotations

import importlib.util
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from PIL.Image import Image

ROOT = Path(__file__).resolve().parents[1]


def _load_make_icons() -> ModuleType:
    """Import the script by path: `packaging` would otherwise resolve to the PyPI package of that name."""
    spec = importlib.util.spec_from_file_location("openberry_make_icons", ROOT / "packaging" / "make_icons.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while the class is created
    spec.loader.exec_module(module)
    return module


mi = _load_make_icons()


@pytest.fixture
def pil() -> ModuleType:
    """Pillow's Image module, or skip the test."""
    return pytest.importorskip("PIL.Image")


def fake_icon(size: int, style: object) -> Image:
    """A Pillow drawing with the mark's layout (brand rounded square, white berry), for tests without a browser."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    inset = style.margin(size)  # type: ignore[attr-defined]
    scale = (size - 2 * inset) / 32
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((inset, inset, size - inset - 1, size - inset - 1), radius=9 * scale,
                           fill=(*mi.BRAND_RGB, 255))
    cx, cy, r = inset + 16 * scale, inset + 18.5 * scale, 7.5 * scale
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*mi.BERRY_RGB, 255))
    return image


def fake_icon_set() -> tuple[dict[int, Image], dict[int, Image]]:
    macos = {size: fake_icon(size, mi.MACOS) for size in {*mi.PNG_SIZES, *mi.ICNS_SIZES}}
    windows = {size: fake_icon(size, mi.WINDOWS) for size in mi.ICO_SIZES}
    return macos, windows


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


# --- the mark and its SVG ------------------------------------------------------------------------------

def test_read_mark_splits_the_favicon_into_viewbox_and_body() -> None:
    mark = mi.read_mark()
    assert mark.view_box == "0 0 32 32"
    assert "<circle" in mark.body and "#b0175c" in mark.body
    assert "<svg" not in mark.body


def test_read_mark_rejects_a_file_that_is_not_one_svg(tmp_path: Path) -> None:
    bad = tmp_path / "bad.svg"
    bad.write_text("<p>not an svg</p>", encoding="utf-8")
    with pytest.raises(ValueError, match="viewBox"):
        mi.read_mark(bad)
    bad.write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>', encoding="utf-8")
    with pytest.raises(ValueError, match="viewBox"):
        mi.read_mark(bad)


def test_margins_are_whole_pixels_and_small_windows_icons_use_every_pixel() -> None:
    assert mi.MACOS.margin(1024) == 100  # Apple's 824 px icon body
    assert mi.MACOS.margin(256) == 25
    assert [mi.WINDOWS.margin(size) for size in (16, 24, 32)] == [0, 0, 0]
    assert mi.WINDOWS.margin(48) == 3
    assert mi.WINDOWS.margin(256) == 16


def test_icon_svg_centres_the_mark_and_only_macos_gets_a_shadow() -> None:
    mark = mi.read_mark()
    macos = mi.icon_svg(mark, 1024, mi.MACOS)
    windows = mi.icon_svg(mark, 16, mi.WINDOWS)
    for svg, size in ((macos, 1024), (windows, 16)):
        root = ET.fromstring(svg)  # well-formed
        assert root.get("width") == root.get("height") == str(size)
    assert 'x="100" y="100" width="824" height="824" viewBox="0 0 32 32"' in macos
    assert "feDropShadow" in macos
    assert 'x="0" y="0" width="16" height="16"' in windows
    assert "filter" not in windows


# --- finding a browser ---------------------------------------------------------------------------------

def test_find_chromium_prefers_the_newest_build_and_full_chromium_over_the_headless_shell(tmp_path: Path) -> None:
    _touch(tmp_path / "chromium-1100" / "chrome-linux" / "chrome")
    _touch(tmp_path / "chromium_headless_shell-1194" / "chrome-linux" / "headless_shell")
    full = _touch(tmp_path / "chromium-1194" / "chrome-linux" / "chrome")
    _touch(tmp_path / "firefox-1500" / "firefox" / "firefox")  # not Chromium
    _touch(tmp_path / "chromium-tip" / "chrome-linux" / "chrome")  # no revision number
    assert mi.find_chromium([tmp_path]) == str(full)

    newer_shell = _touch(tmp_path / "chromium_headless_shell-1243" / "chrome-headless-shell-linux64"
                         / "chrome-headless-shell")
    assert mi.find_chromium([tmp_path]) == str(newer_shell)


def test_find_chromium_knows_the_macos_and_windows_layouts(tmp_path: Path) -> None:
    mac = _touch(tmp_path / "mac" / "chromium-1194" / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS"
                 / "Chromium")
    win = _touch(tmp_path / "win" / "chromium-1194" / "chrome-win" / "chrome.exe")
    assert mi.find_chromium([tmp_path / "mac"]) == str(mac)
    assert mi.find_chromium([tmp_path / "win"]) == str(win)


def test_find_chromium_returns_none_when_nothing_is_installed(tmp_path: Path) -> None:
    assert mi.find_chromium([tmp_path / "missing", tmp_path]) is None


def test_playwright_browser_dirs_honour_playwright_browsers_path(monkeypatch: pytest.MonkeyPatch,
                                                                  tmp_path: Path) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert mi.playwright_browser_dirs() == [tmp_path]
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
    dirs = mi.playwright_browser_dirs()
    assert Path.home() / ".cache" / "ms-playwright" in dirs
    assert all(path.name == "ms-playwright" for path in dirs)


# --- writing and checking the files (Pillow, no browser) -----------------------------------------------

def test_write_icons_produces_files_that_pass_the_check(tmp_path: Path, pil: ModuleType) -> None:
    written = mi.write_icons(*fake_icon_set(), tmp_path)
    assert sorted(path.name for path in written) == [
        "openberry-1024.png", "openberry-256.png", "openberry-512.png", "openberry.icns", "openberry.ico"]
    assert mi.check_icons(tmp_path) == []

    with pil.open(tmp_path / "openberry.ico") as ico:
        assert sorted(ico.info["sizes"]) == [(size, size) for size in mi.ICO_SIZES]
    with pil.open(tmp_path / "openberry.icns") as icns:
        icns.load()
        assert icns.format == "ICNS" and icns.size == (1024, 1024)


def test_check_icons_reports_missing_files(tmp_path: Path, pil: ModuleType) -> None:
    problems = mi.check_icons(tmp_path)
    for name in ("openberry-1024.png", "openberry-512.png", "openberry-256.png", "openberry.ico", "openberry.icns"):
        assert f"missing {name}" in problems


def test_check_icons_reports_an_ico_without_every_size(tmp_path: Path, pil: ModuleType) -> None:
    mi.write_icons(*fake_icon_set(), tmp_path)
    fake_icon(256, mi.WINDOWS).save(tmp_path / "openberry.ico", "ICO", sizes=[(32, 32), (256, 256)])
    assert any(problem.startswith("openberry.ico: sizes") for problem in mi.check_icons(tmp_path))


def test_check_image_flags_wrong_size_opaque_corners_and_off_brand_colours(pil: ModuleType) -> None:
    good = fake_icon(256, mi.MACOS)
    assert mi.check_image(good, 256, mi.MACOS, "good") == []
    assert mi.check_image(good, 512, mi.MACOS, "small") == ["small: size (256, 256), expected 512x512"]

    blue = pil.new("RGBA", (256, 256), (0, 0, 255, 255))
    problems = mi.check_image(blue, 256, mi.MACOS, "blue")
    assert "blue: top-left corner is not transparent" in problems
    assert any("brand colour" in problem for problem in problems)
    assert any("white berry" in problem for problem in problems)
    assert mi.check_image(blue, 256, mi.MACOS, "blue", colours=False) == [
        "blue: top-left corner is not transparent"]


def test_the_committed_icons_pass_the_check_and_stay_small(pil: ModuleType) -> None:
    assert mi.check_icons() == []
    assert mi.main(["--check"]) == 0
    for path in mi.ICONS_DIR.iterdir():
        assert path.stat().st_size < 150_000, path.name


def test_main_reports_a_missing_browser_with_exit_code_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    def no_browser(*_args: object) -> list[Path]:
        raise RuntimeError("could not start Chromium (gone)")

    monkeypatch.setattr(mi, "build", no_browser)
    assert mi.main(["--out", str(tmp_path)]) == 2
    assert "error: could not start Chromium (gone)" in capsys.readouterr().err


# --- rendering (needs Playwright and a Chromium; skipped otherwise) ------------------------------------

def test_rendering_the_favicon_gives_a_crisp_on_brand_icon(pil: ModuleType) -> None:
    pytest.importorskip("playwright.sync_api")
    mark = mi.read_mark()
    try:
        icons = mi.render_styles(mark, [(mi.WINDOWS, (32,)), (mi.MACOS, (64,))])
    except RuntimeError as exc:
        pytest.skip(str(exc))
    windows, macos = icons[0][32], icons[1][64]
    assert mi.check_image(windows, 32, mi.WINDOWS, "windows@32") == []
    assert mi.check_image(macos, 64, mi.MACOS, "macos@64") == []
    # Full bleed at 32 px: the berry square reaches the middle of every edge.
    assert windows.getpixel((0, 16))[3] == windows.getpixel((31, 16))[3] == 255
