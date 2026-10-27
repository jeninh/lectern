# Save web articles

Read a long web article later on your e-reader: paste its link (or share it from the
installed app) and Lectern turns the article into a clean EPUB, files it as a book, and
puts it in your library like any other. From there it is in the catalogue on your Kindle or
Kobo, on shelves, and in [Send to device](device-queues.md).

## Saving one

**Library → Add books ▾ → Save a web article…**, paste the link, **Save**. On a phone with
Lectern installed, use the browser's **Share → Lectern** instead. The list under the box
shows each save: working, saved (with a link to the book), or why it failed.

The book gets:

- the article's text, headings, lists, quotes, tables, and links (links still go to the
  web), with the page's menus, sharing buttons, comments, related stories, and adverts
  left out;
- its pictures, made smaller for e-readers (at most 40, 40 MB together; ones that do not
  fit are left out and counted);
- title, author (or the site's name), site, date, and language from the page, the lead
  image as the cover, the subject *Article*, and the link it came from.

Saving the same article again finds the book you already have. A site that builds its
pages with JavaScript, or puts articles behind a login, has nothing for Lectern to read: the
save fails with *no readable article*.

## What is safe about it

The server fetches the page, so it only fetches from the public internet:

- only `http` and `https` links, with no passwords in them;
- the site's name is looked up and **every** address it gives must be public: no loopback,
  home network (192.168.x.x, 10.x.x.x, 172.16–31.x.x), link-local (cloud metadata),
  Tailscale (100.64.0.0/10 and its IPv6 range), or other private and reserved ranges;
- the connection goes to the address that was checked (a second lookup cannot swap in a
  private one), and every redirect is checked again, at most five;
- the page may be at most 5 MB, each picture 8 MB (compressed data counts as unpacked), and
  the whole save 2 minutes;
- scripts, frames, forms, embedded objects, event handlers, styles that load anything, and
  remote resources are removed. The EPUB holds only the article and pictures Lectern
  downloaded and re-encoded itself, and passes EPUBCheck.

Saving runs as a job on the server's queue (a Raspberry Pi is fine) and is never sent to a
worker.

## Offline mode

Saving needs the internet. While [offline mode](offline-mode.md) is on it is off, and the
save box says so; nothing is fetched.
