import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowUpIcon, MessagesSquareIcon } from "lucide-react";
import { getCurrentAuthorId } from "@/lib/identity";
import {
  type Bubble,
  type BubbleCache,
  buildBubbles,
  createBubbleCache,
  liveCandidateAssistantIndex,
} from "@/lib/renderItems";
import {
  BubbleView,
  WorkingIndicator,
  bubbleKey,
  buildPendingBubbles,
  computeIsWorking,
  mergePendingBubbles,
  reorderCommittedRequestElicitations,
  shouldShowWorkingIndicator,
  stripGatedSubagentRoutingChips,
} from "@/components/chat/chatBubbleParts";
import { ChatComposer } from "@/components/composer/ChatComposer";
import { Button } from "@/components/ui/button";
import { useChatStore, ensureConversationStreamed } from "@/store/chatStore";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";

/**
 * A scoped chat surface for a side-chat child, rendered as a Workspace-rail tab
 * beside the still-active main chat. Reads the child conversation's live state
 * straight from its registry entry (not the root store, which only projects the
 * active conversation) and reuses the main transcript's bubble pipeline
 * (`buildBubbles` + `BubbleView`) in a simple, non-virtualized column — side
 * chats stay short. Follow-up turns are sent to the child via
 * `send(..., { pinnedConversationId })`, so they land on the side thread, never
 * the main one.
 *
 * @param childId The side-chat child conversation id backing this tab.
 */
export function SideChatPane({ childId }: { childId: string }) {
  // Open the child's stream once (without making it the active conversation) so
  // its transcript hydrates and streams here. The store guards against a
  // double-bind, so re-mounts / tab switches are cheap.
  useEffect(() => {
    void ensureConversationStreamed(childId);
  }, [childId]);

  const state = useConversationEntryState(childId);
  const {
    blocks,
    activeResponse,
    interruptedResponseIds,
    pendingUserMessages,
    subagentRoutingOverride,
    sessionStatus,
    boundAgentId,
    loadingConversation,
  } = state;

  const bubbleCacheRef = useRef<BubbleCache>(createBubbleCache());
  const bubbles = useMemo<Bubble[]>(() => {
    const committed = stripGatedSubagentRoutingChips(
      reorderCommittedRequestElicitations(
        buildBubbles(
          blocks,
          activeResponse,
          bubbleCacheRef.current,
          interruptedResponseIds,
          computeIsWorking(sessionStatus),
        ),
      ),
      subagentRoutingOverride,
    );
    if (pendingUserMessages.length === 0) return committed;
    return mergePendingBubbles(
      committed,
      buildPendingBubbles(pendingUserMessages, getCurrentAuthorId()),
    );
  }, [
    blocks,
    activeResponse,
    interruptedResponseIds,
    pendingUserMessages,
    subagentRoutingOverride,
    sessionStatus,
  ]);

  const lastAssistantIndex = liveCandidateAssistantIndex(bubbles);
  const showsWorking = computeIsWorking(sessionStatus);

  // Keep the newest content in view. A side chat is short and non-virtualized,
  // so a bottom sentinel scrolled on each change is enough (no stick-to-bottom
  // machinery).
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [bubbles.length, activeResponse]);

  const isEmpty = bubbles.length === 0 && !loadingConversation;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-3 py-4">
        {isEmpty ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
            <MessagesSquareIcon className="size-6 text-muted-foreground" />
            <p className="text-ui font-medium text-foreground">Side chat</p>
            <p className="max-w-[36ch] text-sm text-muted-foreground">
              Side chats are temporary and disappear when you close the app.
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            {bubbles.map((bubble, index) => (
              <BubbleView
                key={bubbleKey(bubble)}
                bubble={bubble}
                isLastAssistant={index === lastAssistantIndex}
                showsWorking={showsWorking}
              />
            ))}
            {shouldShowWorkingIndicator(showsWorking, bubbles) && <WorkingIndicator />}
            <div ref={bottomRef} />
          </div>
        )}
      </div>
      <div className="shrink-0 p-3">
        <SideChatComposer childId={childId} agentId={boundAgentId} busy={showsWorking} />
      </div>
    </div>
  );
}

/**
 * The side chat's own composer. Sends each turn to the child conversation via
 * `pinnedConversationId`, so the message and its optimistic bubble land on the
 * side thread instead of the main chat. A minimal single-line-growing textarea
 * with a send button — the side chat's full model/effort controls are a
 * follow-up.
 */
function SideChatComposer({
  childId,
  agentId,
  busy,
}: {
  childId: string;
  agentId: string | null;
  busy: boolean;
}) {
  const send = useChatStore((s) => s.send);
  const clearSideChatDraft = useChatStore((s) => s.clearSideChatDraft);
  const [text, setText] = useState("");
  // Seed the composer from a `/side <question>` that opened this side chat, so a
  // generic side chat doesn't lose the typed question. Consumed once on mount.
  useEffect(() => {
    const draft = useChatStore.getState().sideChatDrafts[childId];
    if (draft) {
      setText(draft);
      clearSideChatDraft(childId);
    }
  }, [childId, clearSideChatDraft]);
  const canSend = text.trim().length > 0 && agentId !== null;

  const submit = () => {
    const trimmed = text.trim();
    if (trimmed.length === 0 || agentId === null) return;
    setText("");
    void send(trimmed, agentId, [], { pinnedConversationId: childId });
  };

  return (
    <ChatComposer
      keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
      input={{
        value: text,
        onChange: (event) => setText(event.target.value),
        placeholder: "Ask a side question...",
        "data-testid": "side-chat-input",
        onKeyDown: (event, intent) => {
          if (intent.shouldSubmitFromKeyboard) {
            event.preventDefault();
            submit();
          }
        },
      }}
      actions={{
        leading: null,
        trailing: (
          <Button
            type="button"
            size="icon"
            variant="default"
            aria-label="Send side question"
            disabled={!canSend || busy}
            onClick={submit}
            data-testid="side-chat-send"
          >
            <ArrowUpIcon className="size-4" />
          </Button>
        ),
      }}
    />
  );
}
