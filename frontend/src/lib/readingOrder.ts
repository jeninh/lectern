// Two separate per-user lists (issue #70): "Next in series" is computed by the server from the
// series you have started; "Want to read" is a queue you curate and the server never fills.

import { del, get, post, put } from "./api";
import type { Work } from "./types";

type Items = { items: Work[] };

export const nextInSeries = () => get<Items>("/api/next-in-series").then((x) => x.items);
export const wantToRead = () => get<Items>("/api/want-to-read").then((x) => x.items);
export const addWantToRead = (workId: number) => post<Items>("/api/want-to-read", { work_id: workId }).then((x) => x.items);
export const removeWantToRead = (workId: number) => del<Items>(`/api/want-to-read/${workId}`).then((x) => x.items);
export const reorderWantToRead = (workIds: number[]) => put<Items>("/api/want-to-read/order", { work_ids: workIds }).then((x) => x.items);
