import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { readSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { useSideChats } from "./useSideChats";

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("side-chat soft tabs", () => {
  it("opens child ids as tabs, selecting each, and is idempotent", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    expect(result.current.tabs).toEqual([]);
    act(() => result.current.open("conv_side1"));
    expect(result.current.tabs).toEqual(["conv_side1"]);
    expect(result.current.selected).toBe("conv_side1");
    act(() => result.current.open("conv_side2"));
    expect(result.current.tabs).toEqual(["conv_side1", "conv_side2"]);
    expect(result.current.selected).toBe("conv_side2");
    // Re-opening an existing child re-selects it without duplicating the tab
    // (a create both returns the id and fires a session_created).
    act(() => result.current.open("conv_side1"));
    expect(result.current.tabs).toEqual(["conv_side1", "conv_side2"]);
    expect(result.current.selected).toBe("conv_side1");
  });

  it("persists tabs and selection across remount, isolated per session", () => {
    const first = renderHook(() => useSideChats("session-a"));
    act(() => first.result.current.open("conv_a1"));
    act(() => first.result.current.open("conv_a2"));
    first.unmount();

    const restored = renderHook(() => useSideChats("session-a"));
    expect(restored.result.current.tabs).toEqual(["conv_a1", "conv_a2"]);
    expect(restored.result.current.selected).toBe("conv_a2");
    expect(readSessionWorkspaceState("session-a")).toEqual({
      openSideChats: ["conv_a1", "conv_a2"],
      selectedSideChatId: "conv_a2",
    });

    const other = renderHook(() => useSideChats("session-b"));
    expect(other.result.current.tabs).toEqual([]);
  });

  it("closes the target tab and selects a neighbor, then clears", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    act(() => result.current.open("conv_1"));
    act(() => result.current.open("conv_2"));
    act(() => result.current.close("conv_2"));
    expect(result.current.tabs).toEqual(["conv_1"]);
    expect(result.current.selected).toBe("conv_1");
    act(() => result.current.close("conv_1"));
    expect(result.current.tabs).toEqual([]);
    expect(result.current.selected).toBeNull();
  });

  it("keeps the selection when closing a background tab", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    act(() => result.current.open("conv_1"));
    act(() => result.current.open("conv_2"));
    // conv_2 is selected; closing the background conv_1 leaves it selected.
    act(() => result.current.close("conv_1"));
    expect(result.current.tabs).toEqual(["conv_2"]);
    expect(result.current.selected).toBe("conv_2");
  });

  it("select changes the active tab and can clear it", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    act(() => result.current.open("conv_1"));
    act(() => result.current.open("conv_2"));
    act(() => result.current.select("conv_1"));
    expect(result.current.selected).toBe("conv_1");
    act(() => result.current.select(null));
    expect(result.current.selected).toBeNull();
    // Tabs are untouched by selection changes.
    expect(result.current.tabs).toEqual(["conv_1", "conv_2"]);
  });
});
