import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement>;
const base = { fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round", viewBox: "0 0 24 24", "aria-hidden": true } as const;

export const Home = (p: P) => (
  <svg {...base} {...p}><path d="M3 11.5 12 4l9 7.5" /><path d="M5 10v10h14V10" /></svg>
);
export const Library = (p: P) => (
  <svg {...base} {...p}><path d="M4 4h4v16H4zM10 4h4v16h-4z" /><path d="m16 5 4-1 1 15-4 1z" /></svg>
);
export const Devices = (p: P) => (
  <svg {...base} {...p}><rect x="3" y="5" width="13" height="14" rx="2" /><rect x="17" y="8" width="4" height="11" rx="1" /></svg>
);
export const Shield = (p: P) => (
  <svg {...base} {...p}><path d="M12 3 4 6v6c0 4.5 3.5 8 8 9 4.5-1 8-4.5 8-9V6z" /><path d="m9 12 2 2 4-4" /></svg>
);
export const Pulse = (p: P) => (
  <svg {...base} {...p}><path d="M3 12h4l2-6 4 12 2-6h6" /></svg>
);
export const Users = (p: P) => (
  <svg {...base} {...p}><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20a6.5 6.5 0 0 1 13 0" /><path d="M16 4a3.5 3.5 0 0 1 0 7M18 13.5a6 6 0 0 1 3.5 6.5" /></svg>
);
export const Search = (p: P) => (
  <svg {...base} {...p}><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4-4" /></svg>
);
export const Sun = (p: P) => (
  <svg {...base} {...p}><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>
);
export const Moon = (p: P) => (
  <svg {...base} {...p}><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" /></svg>
);
export const Close = (p: P) => (
  <svg {...base} {...p}><path d="m6 6 12 12M18 6 6 18" /></svg>
);
export const Chevron = (p: P) => (
  <svg {...base} {...p}><path d="m6 9 6 6 6-6" /></svg>
);
export const Sync = (p: P) => (
  <svg {...base} {...p}><path d="M20 11a8 8 0 0 0-14.5-3M4 13a8 8 0 0 0 14.5 3" /><path d="M20 4v4h-4M4 20v-4h4" /></svg>
);
export const Download = (p: P) => (
  <svg {...base} {...p}><path d="M12 4v11M7 10l5 5 5-5" /><path d="M4 19h16" /></svg>
);
export const Send = (p: P) => (
  <svg {...base} {...p}><path d="M4 12h11" /><path d="m11 6 6 6-6 6" /><path d="M20 5v14" /></svg>
);
export const Plus = (p: P) => (
  <svg {...base} {...p}><path d="M12 5v14M5 12h14" /></svg>
);
export const Check = (p: P) => (
  <svg {...base} {...p}><path d="m5 12 4.5 4.5L19 7" /></svg>
);
export const Key = (p: P) => (
  <svg {...base} {...p}><circle cx="8" cy="14" r="4" /><path d="m11 11 9-9M16 6l2 2M13 9l2 2" /></svg>
);
export const Book = (p: P) => (
  <svg {...base} {...p}><path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z" /><path d="M4 19a2 2 0 0 0 2 2h13" /></svg>
);
export const Inbox = (p: P) => (
  <svg {...base} {...p}><path d="M4 13V5h16v8" /><path d="M4 13h4l2 3h4l2-3h4v6H4z" /></svg>
);
export const Login = (p: P) => (
  <svg {...base} {...p}><path d="M10 7V5a2 2 0 0 1 2-2h7v18h-7a2 2 0 0 1-2-2v-2" /><path d="M3 12h11M10 8l4 4-4 4" /></svg>
);
export const Logout = (p: P) => (
  <svg {...base} {...p}><path d="M14 7V5a2 2 0 0 0-2-2H5v18h7a2 2 0 0 0 2-2v-2" /><path d="M21 12H10M17 8l4 4-4 4" /></svg>
);
export const Alert = (p: P) => (
  <svg {...base} {...p}><path d="M12 3 2 20h20z" /><path d="M12 10v4M12 17h.01" /></svg>
);
export const Copy = (p: P) => (
  <svg {...base} {...p}><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" /></svg>
);
export const Command = (p: P) => (
  <svg {...base} {...p}><path d="M9 6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3z" /></svg>
);
export const Help = (p: P) => (
  <svg {...base} {...p}><circle cx="12" cy="12" r="9" /><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.4-1 1-1 1.7M12 17h.01" /></svg>
);
export const Headphones = (p: P) => (
  <svg {...base} {...p}><path d="M4 15v-3a8 8 0 0 1 16 0v3" /><rect x="3" y="14" width="4" height="7" rx="1.5" /><rect x="17" y="14" width="4" height="7" rx="1.5" /></svg>
);
export const Highlighter = (p: P) => (
  <svg {...base} {...p}><path d="m14 4 6 6-8.5 8.5H6v-5.5z" /><path d="M4 21h8" /></svg>
);
export const Upload = (p: P) => (
  <svg {...base} {...p}><path d="M12 16V4M7 9l5-5 5 5" /><path d="M4 19h16" /></svg>
);
export const Sidebar = (p: P) => (
  <svg {...base} {...p}><rect x="3" y="4" width="18" height="16" rx="2.5" /><path d="M9 4v16" /></svg>
);
export const More = (p: P) => (
  <svg {...base} {...p}><circle cx="5" cy="12" r="1.3" fill="currentColor" /><circle cx="12" cy="12" r="1.3" fill="currentColor" /><circle cx="19" cy="12" r="1.3" fill="currentColor" /></svg>
);
export const Gear = (p: P) => (
  <svg {...base} {...p}><circle cx="12" cy="12" r="3" /><path d="M12 2.5v3M12 18.5v3M4.2 6l2.1 2.1M17.7 15.9l2.1 2.1M2.5 12h3M18.5 12h3M4.2 18l2.1-2.1M17.7 8.1l2.1-2.1" /></svg>
);
export const Reader = (p: P) => (
  <svg {...base} {...p}><rect x="5" y="2.5" width="14" height="19" rx="2.5" /><path d="M8.5 7h7M8.5 10.5h7M8.5 14h4" /></svg>
);
export const Person = (p: P) => (
  <svg {...base} {...p}><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></svg>
);
export const Bell = (p: P) => (
  <svg {...base} {...p}><path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z" /><path d="M10 20.5a2 2 0 0 0 4 0" /></svg>
);
export const Play = (p: P) => (
  <svg {...base} {...p}><path d="M7 4.5v15l12.5-7.5z" fill="currentColor" /></svg>
);
export const Pause = (p: P) => (
  <svg {...base} {...p}><rect x="6" y="4.5" width="4" height="15" rx="1" fill="currentColor" /><rect x="14" y="4.5" width="4" height="15" rx="1" fill="currentColor" /></svg>
);
export const PrevSentence = (p: P) => (
  <svg {...base} {...p}><path d="M6 5v14" /><path d="M18 5.5v13L9 12z" /></svg>
);
export const NextSentence = (p: P) => (
  <svg {...base} {...p}><path d="M18 5v14" /><path d="M6 5.5v13L15 12z" /></svg>
);
export const Follow = (p: P) => (
  <svg {...base} {...p}><path d="M4 6h10M4 10h16M4 14h8M4 18h12" /><circle cx="18.5" cy="16" r="2.5" /></svg>
);
export const Back = (p: P) => (
  <svg {...base} {...p}><path d="M4 12a8 8 0 1 0 2.4-5.7" /><path d="M4 4v4.5h4.5" /><text x="12" y="15.2" fontSize="7.5" textAnchor="middle" fill="currentColor" stroke="none" fontFamily="system-ui" fontWeight="600">15</text></svg>
);
export const Forward = (p: P) => (
  <svg {...base} {...p}><path d="M20 12a8 8 0 1 1-2.4-5.7" /><path d="M20 4v4.5h-4.5" /><text x="12" y="15.2" fontSize="7.5" textAnchor="middle" fill="currentColor" stroke="none" fontFamily="system-ui" fontWeight="600">30</text></svg>
);
export const PrevChapter = (p: P) => (
  <svg {...base} {...p}><path d="M6 5v14" /><path d="M18 5v14L8 12z" fill="currentColor" /></svg>
);
export const NextChapter = (p: P) => (
  <svg {...base} {...p}><path d="M18 5v14" /><path d="M6 5v14l10-7z" fill="currentColor" /></svg>
);
export const Timer = (p: P) => (
  <svg {...base} {...p}><circle cx="12" cy="13" r="8" /><path d="M12 9v4l2.5 2.5M9.5 2.5h5" /></svg>
);
export const TextSize = (p: P) => (
  <svg {...base} {...p}><path d="M3 19 8 6l5 13M4.8 14.5h6.4" /><path d="M14 19l3.5-9 3.5 9M15.2 16h4.6" /></svg>
);
export const Toc = (p: P) => (
  <svg {...base} {...p}><path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01" /></svg>
);
export const ArrowLeft = (p: P) => (
  <svg {...base} {...p}><path d="M19 12H5M11 6l-6 6 6 6" /></svg>
);
export const Expand = (p: P) => (
  <svg {...base} {...p}><path d="m6 15 6-6 6 6" /></svg>
);
export const Shelf = (p: P) => (
  <svg {...base} {...p}><path d="M3 20h18M5 20V8h3v12M10 20V4h3v16M15.5 20l-1-11 3-.3 1 11.3" /></svg>
);
export const Link2 = (p: P) => (
  <svg {...base} {...p}><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" /><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" /></svg>
);
export const Folder = (p: P) => (
  <svg {...base} {...p}><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.5h7.5A2.5 2.5 0 0 1 21 10v7.5a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z" /></svg>
);
export const Pencil = (p: P) => (
  <svg {...base} {...p}><path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z" /><path d="m13.5 6.5 4 4" /></svg>
);
export const Trash = (p: P) => (
  <svg {...base} {...p}><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12.5A1.5 1.5 0 0 0 8.5 21h7a1.5 1.5 0 0 0 1.5-1.5L18 7M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7" /></svg>
);
export const Retry = (p: P) => (
  <svg {...base} {...p}><path d="M4 12a8 8 0 1 0 2.4-5.7L4 8.5" /><path d="M4 4v4.5h4.5" /></svg>
);
export const Undo = (p: P) => (
  <svg {...base} {...p}><path d="M9 14 4 9l5-5" /><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" /></svg>
);
export const Sparkle = (p: P) => (
  <svg {...base} {...p}><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6" /></svg>
);
export const Shuffle = (p: P) => (
  <svg {...base} {...p}><path d="M3 7h3.5c2 0 3.3 1 4.5 3l2 4c1.2 2 2.5 3 4.5 3H21" /><path d="M3 17h3.5c1.4 0 2.5-.5 3.4-1.5M14.1 8.5c.9-1 2-1.5 3.4-1.5H21" /><path d="m18 4 3 3-3 3M18 14l3 3-3 3" /></svg>
);
export const Up = (p: P) => (
  <svg {...base} {...p}><path d="m6 15 6-6 6 6" /></svg>
);
export const Down = (p: P) => (
  <svg {...base} {...p}><path d="m6 9 6 6 6-6" /></svg>
);
export const Select = (p: P) => (
  <svg {...base} {...p}><rect x="3.5" y="3.5" width="17" height="17" rx="4" /><path d="m8 12 3 3 5-6" /></svg>
);
export const Filter = (p: P) => (
  <svg {...base} {...p}><path d="M4 5h16l-6.2 7.4V19l-3.6-1.8v-4.8z" /></svg>
);
/** A smart shelf: a folder that fills itself from a saved filter. */
export const SmartFolder = (p: P) => (
  <svg {...base} {...p}><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.5h7.5A2.5 2.5 0 0 1 21 10v7.5a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z" /><path d="M8.5 10.5h7l-2.8 3.1v2.6l-1.4-.7v-1.9z" /></svg>
);
