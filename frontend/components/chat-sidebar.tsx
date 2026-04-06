"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, MessageSquarePlus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

const ORCHESTRATOR_URL =
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ?? "http://localhost:8000";

const USE_CHAT_PROXY =
  process.env.NEXT_PUBLIC_USE_CHAT_PROXY === "1" ||
  process.env.NEXT_PUBLIC_USE_CHAT_PROXY === "true";

const ACCESS_TOKEN_STORAGE_KEY = "pa-orchestrator-access-token";

function sessionsBaseUrl(): string {
  if (USE_CHAT_PROXY) return "/api/chat/sessions";
  return `${ORCHESTRATOR_URL}/chat/sessions`;
}

function sessionDeleteUrl(id: string): string {
  if (USE_CHAT_PROXY) return `/api/chat/sessions/${encodeURIComponent(id)}`;
  return `${ORCHESTRATOR_URL}/chat/sessions/${encodeURIComponent(id)}`;
}

function orchestratorAuthHeaders(): Record<string, string> {
  if (typeof window === "undefined") return {};
  try {
    const t = sessionStorage.getItem(ACCESS_TOKEN_STORAGE_KEY);
    if (t) return { Authorization: `Bearer ${t}` };
  } catch {
    /* ignore */
  }
  return {};
}

export type ChatSessionRow = {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  cleared_at: string | null;
};

type ChatSessionsPage = {
  items: ChatSessionRow[];
  next_cursor: string | null;
};

function formatSessionLabel(s: ChatSessionRow): string {
  const t = (s.title ?? "").trim();
  if (t) return t.length > 42 ? `${t.slice(0, 40)}…` : t;
  try {
    const d = new Date(s.updated_at);
    if (!Number.isNaN(d.getTime())) {
      return `Chat · ${d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })}`;
    }
  } catch {
    /* ignore */
  }
  return `Chat · ${s.id.slice(0, 8)}…`;
}

export type ChatSidebarProps = {
  activeSessionId: string | null;
  onSelectSession: (id: string) => void;
  onNewChat: () => void;
  /** Increment or change to refetch the first page of sessions. */
  listRefreshKey: number;
  /** True while a chat stream is in flight — block switching threads. */
  streamBusy: boolean;
  /** Called after a session was deleted on the server (parent may clear active chat). */
  onSessionDeleted: (id: string) => void;
};

export function ChatSidebar({
  activeSessionId,
  onSelectSession,
  onNewChat,
  listRefreshKey,
  streamBusy,
  onSessionDeleted,
}: ChatSidebarProps) {
  const [items, setItems] = useState<ChatSessionRow[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadMoreBusy, setLoadMoreBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchPage = useCallback(
    async (cursor: string | null, append: boolean) => {
      const qs = new URLSearchParams({ limit: "20" });
      if (cursor) qs.set("cursor", cursor);
      const r = await fetch(`${sessionsBaseUrl()}?${qs}`, {
        method: "GET",
        cache: "no-store",
        headers: {
          Accept: "application/json",
          ...(USE_CHAT_PROXY ? {} : orchestratorAuthHeaders()),
        },
      });
      if (!r.ok) {
        const t = await r.text();
        throw new Error(t || `HTTP ${r.status}`);
      }
      const data = (await r.json()) as ChatSessionsPage;
      setNextCursor(data.next_cursor ?? null);
      setItems((prev) => (append ? [...prev, ...data.items] : data.items));
    },
    []
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        await fetchPage(null, false);
      } catch (e) {
        if (!cancelled) {
          setError(
            e instanceof Error ? e.message : "Could not load conversations."
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [fetchPage, listRefreshKey]);

  const loadMore = async () => {
    if (!nextCursor || loadMoreBusy) return;
    setLoadMoreBusy(true);
    setError(null);
    try {
      await fetchPage(nextCursor, true);
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Could not load more conversations."
      );
    } finally {
      setLoadMoreBusy(false);
    }
  };

  const deleteSession = async (e: React.MouseEvent, id: string) => {
    e.preventDefault();
    e.stopPropagation();
    if (streamBusy) return;
    if (!confirm("Delete this chat permanently?")) return;
    try {
      const r = await fetch(sessionDeleteUrl(id), {
        method: "DELETE",
        headers: {
          ...(USE_CHAT_PROXY ? {} : orchestratorAuthHeaders()),
        },
      });
      if (!r.ok) {
        const t = await r.text();
        setError(t || `Delete failed (${r.status})`);
        return;
      }
      onSessionDeleted(id);
      setItems((prev) => prev.filter((x) => x.id !== id));
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not delete conversation."
      );
    }
  };

  return (
    <aside
      className={cn(
        "hidden h-[min(640px,72vh)] w-full shrink-0 flex-col rounded-2xl border border-border/70 bg-card/90 shadow-sm ring-1 ring-black/[0.04] dark:ring-white/[0.06] md:flex md:h-[min(680px,78vh)] md:w-64"
      )}
      aria-label="Chat history"
    >
      <div className="flex items-center justify-between gap-2 border-b border-border/70 px-3 py-2.5">
        <span className="text-xs font-semibold uppercase tracking-[0.18em] text-muted-foreground">
          Chats
        </span>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8 gap-1.5 rounded-lg px-2 text-xs font-medium"
          onClick={() => {
            if (streamBusy) return;
            onNewChat();
          }}
          disabled={streamBusy}
          title="Start a new conversation"
        >
          <MessageSquarePlus className="h-3.5 w-3.5" aria-hidden />
          New
        </Button>
      </div>

      {error && (
        <p className="border-b border-destructive/20 bg-destructive/[0.06] px-3 py-2 text-[11px] leading-snug text-destructive">
          {error}
        </p>
      )}

      <ScrollArea className="flex-1">
        <div className="p-2">
          {loading && items.length === 0 ? (
            <div className="flex items-center justify-center gap-2 py-10 text-xs text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              Loading…
            </div>
          ) : items.length === 0 ? (
            <p className="px-2 py-8 text-center text-xs text-muted-foreground">
              No saved chats yet. Send a message to create one.
            </p>
          ) : (
            <ul className="flex flex-col gap-1">
              {items.map((s) => {
                const active = activeSessionId === s.id;
                return (
                  <li key={s.id}>
                    <button
                      type="button"
                      disabled={streamBusy}
                      onClick={() => {
                        if (streamBusy) return;
                        onSelectSession(s.id);
                      }}
                      className={cn(
                        "group flex w-full items-start gap-1 rounded-xl border border-transparent px-2 py-2 text-left text-xs transition",
                        active
                          ? "border-primary/25 bg-primary/[0.08] text-foreground"
                          : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
                        streamBusy && "pointer-events-none opacity-60"
                      )}
                    >
                      <span className="min-w-0 flex-1 break-words leading-snug">
                        {formatSessionLabel(s)}
                      </span>
                      <span
                        role="presentation"
                        className="shrink-0 opacity-0 transition group-hover:opacity-100 group-focus-within:opacity-100"
                      >
                        <Button
                          type="button"
                          variant="outline"
                          size="icon"
                          className="h-7 w-7 border-transparent bg-transparent text-muted-foreground hover:border-destructive/30 hover:bg-destructive/[0.06] hover:text-destructive"
                          title="Delete chat"
                          disabled={streamBusy}
                          onClick={(e) => void deleteSession(e, s.id)}
                        >
                          <Trash2 className="h-3.5 w-3.5" aria-hidden />
                        </Button>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </ScrollArea>

      {nextCursor && (
        <div className="border-t border-border/70 p-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 w-full border-dashed text-xs text-muted-foreground"
            onClick={() => void loadMore()}
            disabled={loadMoreBusy || loading}
          >
            {loadMoreBusy ? (
              <>
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                Loading…
              </>
            ) : (
              "Load more"
            )}
          </Button>
        </div>
      )}
    </aside>
  );
}
