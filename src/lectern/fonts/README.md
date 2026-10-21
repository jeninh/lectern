# Cover fonts

Fonts that `lectern.covergen` passes to resvg when it draws generated covers. System fonts
are never used except Noto Serif CJK (`fonts-noto-cjk`), so covers look the same everywhere.

| File | Source (google/fonts @ `47d54ca`) | Instance | Coverage |
| --- | --- | --- | --- |
| `LecternCover-Display.ttf` | `ofl/newsreader/Newsreader[opsz,wght].ttf` | opsz 72, wght 500 | Latin (titles) |
| `LecternCover-Text.ttf` | same | opsz 16, wght 450 | Latin (authors) |
| `LecternCover-NotoSerif.ttf` | `ofl/notoserif/NotoSerif[wdth,wght].ttf` | wdth 100, wght 500 | Latin, Greek, Cyrillic |
| `LecternCover-NotoSansCoptic.ttf` | `ofl/notosanscoptic/NotoSansCoptic-Regular.ttf` | static | Coptic |
| `LecternCover-NotoNaskhArabic.ttf` | `ofl/notonaskharabic/NotoNaskhArabic[wght].ttf` | wght 500 | Arabic |
| `LecternCover-NotoSerifHebrew.ttf` | `ofl/notoserifhebrew/NotoSerifHebrew[wdth,wght].ttf` | wdth 100, wght 500 | Hebrew |
| `LecternCover-NotoSerifDevanagari.ttf` | `ofl/notoserifdevanagari/NotoSerifDevanagari[wdth,wght].ttf` | wdth 100, wght 500 | Devanagari |

Each is a static instance (resvg draws variable fonts at their default instance only), renamed
to the family in the table's file name (`Lectern Cover Display`, `Lectern Noto Serif`, …) so
it never collides with an installed copy. The Newsreader and Noto Serif files are subset to
the scripts listed; the others are complete. All are under the SIL Open Font License 1.1
(`OFL-Newsreader.txt`, `OFL-Noto.txt`); none declares a Reserved Font Name.

Rebuild with fontTools 4.x:

```python
from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer


def build(src, axes, dest, family, unicodes=None):
    f = TTFont(src)
    if "fvar" in f:
        f = instancer.instantiateVariableFont(f, axes, updateFontNames=False)
    if unicodes:
        o = subset.Options()
        o.layout_features = ["*"]
        o.name_IDs = ["*"]
        o.notdef_outline = True
        s = subset.Subsetter(o)
        s.populate(unicodes=unicodes)
        s.subset(f)
    for t in ("STAT", "fvar", "avar"):
        if t in f:
            del f[t]
    names = f["name"]
    names.names = [n for n in names.names if n.nameID not in (1, 2, 4, 6, 16, 17, 21, 22, 25)]
    for nid, val in (
        (1, family),
        (2, "Regular"),
        (4, family + " Regular"),
        (6, family.replace(" ", "") + "-Regular"),
    ):
        names.setName(val, nid, 3, 1, 0x409)
        names.setName(val, nid, 1, 0, 0)
    f["OS/2"].usWeightClass = 400
    f["OS/2"].fsSelection = (f["OS/2"].fsSelection & ~0b1100001) | 0b1000000
    f["head"].macStyle = 0
    f.save(dest)


LATIN = [
    *range(0x20, 0x250),
    *range(0x1E00, 0x1F00),
    *range(0x2000, 0x2070),
    0x20AC,
    0x2116,
    0x2122,
]
GREEK_CYRILLIC = LATIN + [
    *range(0x370, 0x530),
    *range(0x1F00, 0x2000),
    *range(0x2DE0, 0x2E00),
    *range(0xA640, 0xA6A0),
]
```

Newsreader uses `LATIN`, Noto Serif `GREEK_CYRILLIC`; the other fonts are not subset.
