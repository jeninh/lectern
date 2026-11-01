# Guest accounts

A guest is a read-only account for a friend or family member outside the household. A guest sees only the shelves you pick, and nothing else about this library.

## Inviting a guest

**Admin → Household invites → New invite → Guest.** Pick the shelves (ordinary hand-made shelves only; a guest also gets everything nested inside them), an optional end date, and whether downloads and OPDS are allowed. Send the link privately; it works once. The guest chooses a username and password.

## What a guest can do

- Browse and read or listen to the works on their shelves, in the web app. Search covers only those books.
- Keep their own reading position, reader settings, and playback speed.
- With downloads allowed: download those books, and create an app password to browse them in an OPDS reader.

## What a guest never gets

Everything else is refused (`403 guest_forbidden`) or does not exist for them: adding, editing, trashing, or sharing anything; shelves other than theirs; other people's names, progress, or activity; your highlights, notes, ratings, reading stats, and vocabulary; want-to-read, device queues, and Send to device; KOReader pairing and sync keys; AI keys and MCP; settings and the security log. Another work, shelf, or file is `404`, exactly as if it did not exist, and no count, facet, search snippet, or series gap mentions it. Guests are held to this list by route, so a feature added later stays closed to them until it is added to the list on purpose.

## End dates and revoking

After a guest's end date, sign-in stops and every session, app password, and device credential stops working at once. **Admin → Guests** shows each guest's shelves and last activity; **Revoke** disables the account, signs it out everywhere, and removes its shelf grants. You can change a guest's shelves, end date, or downloads setting later; it applies on their next request. Guest sign-ins and downloads appear in the security log, marked as guests.

## Network

Guests reach Lectern over the tailnet only. See [Tailscale](deploy-tailscale.md#guests) for the access rule that limits a guest's devices to Lectern.
