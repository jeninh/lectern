import { del, get, post, put } from "./api";
import type { ListeningQueue } from "./types";

export const listeningQueue = () => get<ListeningQueue>("/api/listening-queue");
const changed = <T>(result: Promise<T>) => result.then((value) => {
  window.dispatchEvent(new Event("lectern:listening-queue"));
  return value;
});

export const enqueueListening = (work_id: number, track_id: number | null = null) =>
  changed(post<ListeningQueue>("/api/listening-queue/items", { work_id, track_id }));
export const removeListening = (itemId: number) => changed(del<ListeningQueue>(`/api/listening-queue/items/${itemId}`));
export const clearListening = () => changed(del<ListeningQueue>("/api/listening-queue"));
export const reorderListening = (item_ids: number[]) => changed(put<ListeningQueue>("/api/listening-queue/order", { item_ids }));
