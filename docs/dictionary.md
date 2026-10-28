# Dictionary and translation

Look a word up while you read: **double-click** it (on a phone, **long-press** it) in the
EPUB reader, or select a phrase. On a PDF page, double-click the word. A small popover shows
the word, its part of speech and pronunciation where the dictionary has them, and its
definitions from every dictionary that is turned on for the book's language, plus translations
from bilingual dictionaries into the language you choose at the bottom of the popover
(**Translate into**, remembered on this browser; it starts as your browser's language).

Every dictionary lives on your server. Nothing is looked up on the internet while you read, so
lookups work the same in [offline mode](offline-mode.md), and lookups are not logged.

## How words are matched

- **Case and accents do not matter**: `CAFE` finds *café*.
- **Simple word forms** fall back to the dictionary form: *running* → *run*, *flies* → *fly*,
  *wolves* → *wolf*, *chevaux* → *cheval*, *l'homme* → *homme*. A result found this way says
  which word it was for. English has the most complete rules; French, Spanish, Portuguese,
  Italian, and German have plurals and common endings. Synonym lists that come with a
  dictionary (StarDict `.syn` files) are used too, which covers irregular forms such as *ran*.
- **Phrases first**: a selection of several words (at most six) is looked up as a phrase,
  and, if no dictionary has it, word by word.
- **Which dictionaries**: those for the book's language (its `dc:language`, or the language set
  in the book's editor, which overrides it). A book without a language uses every dictionary.
  Regional variants count as the language: an `en-GB` book uses `en` dictionaries.

Each answer is kept for the rest of the session, so looking a word up again is instant. Each
person can make 120 lookups a minute.

## Adding a dictionary

Owners add dictionaries in **Admin → Dictionaries**. Lectern reads StarDict dictionaries: an `.ifo` file with its `.idx` (or `.idx.gz`) and `.dict` (or `.dict.dz`), and
optionally a `.syn`. Upload them as one `.zip` or `.tar.gz` and say which language the words
are in, and, for a bilingual dictionary, which language the definitions are in. Lectern
converts the dictionary once into an index on the server
(`LECTERN_DATA_DIR/dictionaries/<id>/`), so each lookup is a few index reads, fast on a
Raspberry Pi. Upload one dictionary per archive; archives are limited to 1 GiB.

A dictionary already on the server can be installed from the command line instead (inside the
container with `docker compose exec lectern …`):

```sh
lectern dictionary-import /data/import/wordnet/ --lang en
lectern dictionary-import /data/import/en-fr.tar.gz --lang en --to fr --name "English–French"
lectern dictionary-list
```

Admin lists every dictionary with its languages, entry count, and size. Turn one off to stop
using it without deleting it, or remove it. Lectern never downloads a dictionary by itself:
you fetch the files you want, once, and install them.

Dictionaries are not part of [nightly backups](backups.md); keep the files you installed
from, or reinstall them after a restore.

## Licences

Dictionaries are copyrighted works with their own licences. Lectern ships none, because their
licences vary. When you install one, its licence and readme files (`LICENSE*`, `COPYING*`,
`README*`, `NOTICE*`) are kept with it, and the copyright line from its `.ifo` file is shown in
Admin, so the licence travels with the data.

Sources that are generally suitable for a household server:

- **Wiktionary**: definitions and translations in many languages, under CC BY-SA. StarDict
  conversions are published by several projects; check that the conversion keeps the
  attribution and licence.
- **WordNet** (Princeton): an English dictionary under the WordNet licence, which allows use and
  redistribution with its copyright notice.
- **FreeDict**: bilingual dictionaries, mostly under the GPL or CC BY-SA; each one names its
  licence.

Avoid dictionaries of unclear origin, such as copies of commercial dictionaries.

## Privacy and network

Lookups go only to your Lectern server, over the same private address as everything else
(Tailscale). They are not available on the [home Wi-Fi listener](home-network.md), which
serves e-readers only. The words you look up are not stored or logged.
