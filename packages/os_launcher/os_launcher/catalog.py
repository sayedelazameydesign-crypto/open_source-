"""The Craft App Suite catalog — a faithful Python mirror of the launcher's ``catalog.rs``.

Source of truth: ``packages/craft-launcher/crates/core/src/catalog.rs`` (vendored at
commit ``0f295d4b``). The launcher itself treats the list as data with a built-in
copy; this module is the platform's built-in copy of the same data, so an agent
can answer "what is in the suite?" without compiling Rust.

Nothing here touches the network. Keep the mirror in step with the vendored
``catalog.rs`` when updating ``packages/craft-launcher`` (see ``docs/VENDORED-craft-launcher.md``):
the tests assert the structural invariants (13 apps, ``repo == storytold/<id>``,
PdfCraft's former ``printcraft`` identity) that the Rust ``SUITE`` test asserts.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

#: ArtCraft's website.
WEBSITE_URL = "https://getartcraft.com"
#: The GitHub owner every Craft app lives under.
GITHUB_OWNER = "storytold"
GITHUB_URL = f"https://github.com/{GITHUB_OWNER}"
#: The release feed the launcher prefers over per-repo GitHub polling.
FEED_URL = "https://api.storyteller.ai/v1/craft_apps/release_info"
#: The launcher itself.
LAUNCHER_ID = "artcraft-launcher"
LAUNCHER_REPO = f"{GITHUB_OWNER}/craft-launcher"


class AppGroup(StrEnum):
    """Which generation of the suite an app belongs to."""

    CLASSIC = "classic"
    NEW = "new"

    @property
    def label(self) -> str:
        return {"classic": "Classic", "new": "New"}[self.value]


class CraftApp(BaseModel):
    """One app in the suite, mirroring ``catalog.rs``'s ``CraftApp``."""

    #: Machine name: repo, binary, package and asset stem (``photocraft``).
    id: str
    #: Display name (``PhotoCraft``).
    name: str
    #: What it is for, in a few words (English, the source the UI translates).
    tagline: str
    group: AppGroup
    #: Brand colour (sRGB) for surfaces; text inks for light/dark backgrounds.
    color: tuple[int, int, int]
    ink_light: tuple[int, int, int]
    ink_dark: tuple[int, int, int]
    #: GitHub ``owner/repo``.
    repo: str
    #: Older names its release assets may carry (PdfCraft shipped as PrintCraft).
    former_ids: list[str] = Field(default_factory=list)
    #: Older display names its installers may have registered.
    former_names: list[str] = Field(default_factory=list)

    def bundle_id(self) -> str:
        """``ai.storyteller.<id>``: macOS bundle id, Flatpak id, desktop-file id."""
        return f"ai.storyteller.{self.id}"

    def repo_url(self) -> str:
        return f"https://github.com/{self.repo}"

    def releases_url(self) -> str:
        return f"{self.repo_url()}/releases"

    def website(self) -> str:
        return f"{WEBSITE_URL}/apps/{self.id}"

    def stems(self) -> list[str]:
        """Asset stems this app's releases may use, current first."""
        return [self.id, *self.former_ids]

    def names(self) -> list[str]:
        """Display names its installers may have registered, current first."""
        return [self.name, *self.former_names]


def _app(
    id: str,
    name: str,
    tagline: str,
    group: AppGroup,
    color: tuple[int, int, int],
    ink_light: tuple[int, int, int],
    ink_dark: tuple[int, int, int],
    repo: str,
) -> CraftApp:
    return CraftApp(
        id=id,
        name=name,
        tagline=tagline,
        group=group,
        color=color,
        ink_light=ink_light,
        ink_dark=ink_dark,
        repo=repo,
    )


#: The Craft App Suite, in display order: seven classic apps, then six new ones.
SUITE: tuple[CraftApp, ...] = (
    _app(
        "photocraft",
        "PhotoCraft",
        "Image editing",
        AppGroup.CLASSIC,
        (0x2F, 0x7B, 0xF5),
        (0x1A, 0x5B, 0xC4),
        (0x7F, 0xB0, 0xFF),
        "storytold/photocraft",
    ),
    _app(
        "vectorcraft",
        "VectorCraft",
        "Vector illustration",
        AppGroup.CLASSIC,
        (0xE8, 0x57, 0x3F),
        (0xB8, 0x3A, 0x24),
        (0xFF, 0x8A, 0x70),
        "storytold/vectorcraft",
    ),
    _app(
        "filmcraft",
        "FilmCraft",
        "Video editing, color and sound",
        AppGroup.CLASSIC,
        (0x8B, 0x5C, 0xF6),
        (0x6A, 0x3F, 0xD6),
        (0xB6, 0x9C, 0xFF),
        "storytold/filmcraft",
    ),
    _app(
        "lightcraft",
        "LightCraft",
        "Photo library and raw development",
        AppGroup.CLASSIC,
        (0xF2, 0xA5, 0x16),
        (0x8A, 0x58, 0x00),
        (0xFF, 0xC1, 0x4D),
        "storytold/lightcraft",
    ),
    CraftApp(
        former_ids=["printcraft"],
        former_names=["PrintCraft"],
        **_app(
            "pdfcraft",
            "PdfCraft",
            "Reading, organizing and protecting PDFs",
            AppGroup.CLASSIC,
            (0x12, 0xA5, 0x8A),
            (0x0A, 0x75, 0x63),
            (0x3F, 0xD6, 0xB8),
            "storytold/pdfcraft",
        ).model_dump(exclude={"former_ids", "former_names"}),
    ),
    _app(
        "effectcraft",
        "EffectCraft",
        "Motion graphics and visual effects",
        AppGroup.CLASSIC,
        (0xE0, 0x36, 0x8F),
        (0xB0, 0x20, 0x6C),
        (0xFF, 0x7A, 0xB8),
        "storytold/effectcraft",
    ),
    _app(
        "designcraft",
        "DesignCraft",
        "Page layout and publishing",
        AppGroup.CLASSIC,
        (0x7B, 0xB5, 0x1C),
        (0x45, 0x70, 0x08),
        (0xA6, 0xE0, 0x4A),
        "storytold/designcraft",
    ),
    _app(
        "wordcraft",
        "WordCraft",
        "Writing and document design",
        AppGroup.NEW,
        (0x3F, 0x5F, 0xE0),
        (0x2A, 0x44, 0xB8),
        (0x8E, 0xA2, 0xFF),
        "storytold/wordcraft",
    ),
    _app(
        "deckcraft",
        "DeckCraft",
        "Presentations and slide shows",
        AppGroup.NEW,
        (0xF0, 0x7A, 0x1E),
        (0xA8, 0x4F, 0x0A),
        (0xFF, 0xA0, 0x60),
        "storytold/deckcraft",
    ),
    _app(
        "gridcraft",
        "GridCraft",
        "Spreadsheets and data",
        AppGroup.NEW,
        (0x1F, 0x9D, 0x55),
        (0x12, 0x70, 0x3A),
        (0x4F, 0xD6, 0x8A),
        "storytold/gridcraft",
    ),
    _app(
        "cadcraft",
        "CADCraft",
        "Computer-aided design and drafting",
        AppGroup.NEW,
        (0x3E, 0xA6, 0xE8),
        (0x1A, 0x6F, 0xA8),
        (0x7C, 0xC8, 0xFF),
        "storytold/cadcraft",
    ),
    _app(
        "soundcraft",
        "SoundCraft",
        "Recording, editing and mixing audio",
        AppGroup.NEW,
        (0x1F, 0xA5, 0xC9),
        (0x0E, 0x72, 0x90),
        (0x5F, 0xD0, 0xEE),
        "storytold/soundcraft",
    ),
    _app(
        "figcraft",
        "FigCraft",
        "Interface design and prototyping",
        AppGroup.NEW,
        (0x02, 0xDA, 0x96),
        (0x04, 0x7A, 0x56),
        (0x4F, 0xE8, 0xB6),
        "storytold/figcraft",
    ),
)


def suite() -> tuple[CraftApp, ...]:
    """The thirteen apps, in display order."""
    return SUITE


def by_id(app_id: str) -> CraftApp | None:
    """Look an app up by its machine id — or by a former id (``printcraft``)."""
    wanted = app_id.strip().lower()
    for app in SUITE:
        if wanted in app.stems():
            return app
    return None
