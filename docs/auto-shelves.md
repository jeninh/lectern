# Auto-shelves

Auto-shelves are shelves the library keeps for itself: one shelf per author, tag, genre,
language, or narrator, made and updated from your books. Add a book by an author you have not
read before and their shelf appears; there is nothing to sort by hand. They work on the Kindle
(OPDS) and in the MCP tools like any other shelf.

## Make them

In the Library choose **New → Auto-shelves…**. Pick what each shelf is for (**Author**, **Tag**,
**Genre**, **Language**, or **Narrator**), a name for the group (*Authors*), and the settings
below. The group goes in the folder you are looking at, or at the top level.

| Setting | What it does |
| --- | --- |
| **Fewest books** | A value needs this many books *you can see* before its shelf appears (1 to 50). Authors and tags start at 2 so one-off values do not crowd the list; the others start at 1. |
| **List shelves by** | **Name**, or **Number of books** (most first). The count is of the books the person looking can see. |
| **Group by first letter** | Puts A–Z shelves (and `#` for names that do not start with a letter) between the group and the values. On by default for authors and tags. |
| **Books ordered by**, **Direction** | How the books inside each generated shelf are sorted. |

Change them later from the group's **⋯ → Auto-shelf settings…**. What each shelf is for (the
field) cannot change; make another group instead.

## Three kinds of shelf

- **Group**: the shelf you made. It is an ordinary shelf: rename it, move it, share it, delete
  it. Deleting a group deletes every shelf under it, with no question about their contents, and
  never touches a book.
- **Bucket**: an A–Z shelf under a group, only when letters are on.
- **Child**: the shelf for one value, such as *Leo Tolstoy*. It is a [smart
  shelf](filters.md#smart-shelves) whose filter is the exact one for the value, so you can see
  it, copy it, and use it in `shelf:` filters. In Cmd/Ctrl+K it appears as *Authors › Leo
  Tolstoy*, below your own shelves.

Buckets and children are kept up to date by the library, so they have no **Rename**, **Move**,
**Sharing**, or **Delete**, cannot be dragged or dropped on, and nothing can be put in them or
under them. A group has no books of its own either: open it to see its buckets or children, or
filter it to search everything below.

## What a shelf matches

A child's filter uses the exact match from [Library filters](filters.md#syntax), so the
author shelf for *Ann* is `author:=Ann` and does not hold Joanne Harris. Accents and case are
ignored, as in any filter. The other fields are `tag:fantasy`, `lang:en`, and the narrator name.

### Genres are tags you map

Books carry tags, not genres. The owner decides what a genre is in **Admin → Genres**: a genre
is a name and the tags that make it up (at most 40 genres, each of at most 20 tags). A **Genre**
group has one child per genre, holding the books with *any* of its tags (`tag:fantasy,epic`).
A tag that is in no genre makes no shelf. Members can see the map; only the owner changes it.

## Who sees what

Shelves are made from the whole library, but a person sees only the shelves for books they can
see. A reader sharing one shelf sees an author's shelf only if at least **Fewest books** of that
author's books are on it for them; a bucket shows only while one of its children does. A shelf
they may not see is absent, exactly as if it did not exist, so shelf names and counts never tell
a reader about books they cannot see.

## Dormancy

When too few people-visible books remain, a shelf is not deleted: it goes **dormant**, hidden
from everyone, and keeps its id. If books come back (a restore from the Trash, or more books by
the author), the **same** shelf returns, so a Kindle or device that was following it keeps
working. A shelf with no books at all and no device following it is removed.

## Names and depth

- Generated names come from the books. A `/` in a value becomes `∕` (shelf names cannot contain
  `/`), control characters are dropped, and a name is at most 128 characters.
- A value the filter syntax cannot express exactly gets no shelf, rather than a shelf that holds
  the wrong books.
- Shelves nest at most 8 deep. A group needs one level for its children, or two with letters, so a
  group at depth *d* needs *d* + 2 ≤ 8 with letters and *d* + 1 ≤ 8 without. The library refuses to
  make or move a group where its shelves would not fit, and refuses to turn letters on when there
  is no room.
- No shelf can be moved into a group, a bucket, or a child, and none of them can be an
  email-in destination, an import's shelf, or a choice in the editor's **Shelves**.
