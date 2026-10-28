# Reader fonts

High-legibility and dyslexia-friendly fonts the web reader offers under **Text and layout →
Font** (`frontend/src/lib/reader.ts`). They come from npm (Fontsource) and Vite copies the
`.woff2` files into `/app/assets`, so the Pi serves them and the service worker precaches
them: a book never asks a font CDN or any other outside server for them, offline included.

| Font | Package | Files | Licence |
| --- | --- | --- | --- |
| OpenDyslexic | `@fontsource/opendyslexic` | latin, 400/700, upright and italic | `OFL-OpenDyslexic.txt` |
| Atkinson Hyperlegible | `@fontsource/atkinson-hyperlegible` | latin and latin-ext, 400/700, upright and italic | `OFL-AtkinsonHyperlegible.txt` |
| Lexend | `@fontsource-variable/lexend` | latin and latin-ext, variable weight | `OFL-Lexend.txt` |

All are under the SIL Open Font License 1.1; the files here are the packages' own `LICENSE`.
OpenDyslexic declares the Reserved Font Name "OpenDyslexic": the files are used as published,
never modified or re-subset here. The reader names the families `Lectern …` in its
`@font-face` rules so they never collide with an installed copy.
