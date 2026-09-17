import { useCallback, useState } from "react";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

interface SideChatTabsState {
  /** Open side-chat child conversation ids, in open order. */
  tabs: string[];
  /** The selected side-chat tab, or null when another rail view is active. */
  selected: string | null;
}

function readSideChatTabsState(conversationId: string): SideChatTabsState {
  const saved = readSessionWorkspaceState(conversationId);
  const tabs = saved.openSideChats ?? [];
  return {
    tabs,
    selected: tabs.includes(saved.selectedSideChatId ?? "") ? saved.selectedSideChatId! : null,
  };
}

/**
 * Per-session side-chat tabs for the Workspace rail — a browser-local list of
 * child conversation ids, mirroring {@link useBrowserTabs}. Browser-local by
 * design: side chats are ephemeral and disappear when the app is closed, so the
 * tab references live only in `sessionWorkspaceState` (localStorage), never on
 * the server.
 *
 * Opening (`open`) just records an already-created child id as a tab — the
 * child itself is created by the server (Codex's native ephemeral fork, or the
 * generic `POST /v1/sessions/{id}/side-chat`) and surfaced here once its
 * `session_created` event arrives.
 *
 * @param conversationId The parent (main) conversation whose rail owns these tabs.
 */
export function useSideChats(conversationId: string) {
  const [state, setState] = useState(() => readSideChatTabsState(conversationId));
  const update = useCallback(
    (mutate: (current: SideChatTabsState) => SideChatTabsState) => {
      const next = mutate(readSideChatTabsState(conversationId));
      writeSessionWorkspaceState(conversationId, {
        openSideChats: next.tabs,
        selectedSideChatId: next.selected,
      });
      setState(next);
    },
    [conversationId],
  );

  /** Select a side-chat tab, or clear the selection (null). */
  const select = useCallback(
    (selected: string | null) => update((current) => ({ ...current, selected })),
    [update],
  );

  /** Add a child id as a tab and select it. Idempotent: an already-open child
   *  is re-selected, not duplicated (a create both returns the id and fires a
   *  `session_created`, so `open` can be reached twice for one child). */
  const open = useCallback(
    (childId: string) =>
      update((current) => ({
        tabs: current.tabs.includes(childId) ? current.tabs : [...current.tabs, childId],
        selected: childId,
      })),
    [update],
  );

  /** Close a tab. Drops only the client-side reference — an ephemeral Codex
   *  fork dies with its process, and a generic child stays hidden server-side. */
  const close = useCallback(
    (childId: string) =>
      update((current) => {
        const index = current.tabs.indexOf(childId);
        if (index === -1) return current;
        const tabs = current.tabs.filter((id) => id !== childId);
        const selected =
          current.selected === childId ? (tabs[Math.max(0, index - 1)] ?? null) : current.selected;
        return { tabs, selected };
      }),
    [update],
  );

  return { tabs: state.tabs, selected: state.selected, open, close, select };
}
