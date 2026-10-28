# Vocabulary list

Words you save while reading are kept in a private list under **Vocabulary** in the sidebar.
Saving is always your choice: looking a word up stores nothing, and only the **Save to
vocabulary** button in the [dictionary](dictionary.md) popover adds a word. The list belongs to
you alone; other people on the server never see it.

## What is kept

Each saved word keeps the word as you selected it, its dictionary form (lemma), the
language (and, for a translation, the language it was translated into), the part of speech,
and the definition you saved. When you save from a book, Lectern also keeps the sentence around
the word and where it was, so **Open in book** takes you back to that passage (the EPUB reader
at the word, or the PDF page with the word found). Saving a word you already have (the same
dictionary form in the same language) adds no duplicate: the first entry is kept, with your
edits, and its **seen N×** count goes up.

If you can no longer see a book (for example, it stopped being shared with you), its saved
words stay in your list, but without the book, the sentence, and the link.

## Finding and editing words

Filter the list by **book**, **language**, and the **date** you saved a word (from and to). The
filters are in the page's address, so a filtered view can be bookmarked. Each row shows the
word, its lemma, the part of speech and language, the definition, your note, the sentence, the
saved date, and the seen count.

- **Edit** changes the definition and adds or clears a note of your own.
- **Delete** removes the word.

## Exporting

**Export for Anki** and **Export CSV** download the words that match the current filters
(with no filters, all of them).

- **Anki**: a tab-separated text file for Anki's *File → Import*. Each word becomes a card with
  the lemma on the front, the definition and your note on the back, and the sentence and book
  title as extra fields. Cards are tagged `lectern`, `lang::<language>`, and `book::<title>`.
- **CSV**: one row per word with its word, lemma, language, translation language, definition,
  note, sentence, book, author, saved date, seen count, and a link back to the passage.

Both files are built on your server; nothing is sent to the internet.

## Privacy

The list is available only when you are signed in, over the same private address as the rest
of Lectern. Words, notes, and sentences are not written to the logs or the audit log.
