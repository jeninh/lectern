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
