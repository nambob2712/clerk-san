import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import type { ExplicitIntakeIntent } from "@/api/contracts";
import {
  UploadQueue,
  createBrowserUploadQueue,
  queueSummary,
  type UploadQueueItem,
} from "@/features/intake/upload-queue";

const UploadQueueContext = createContext<UploadQueue | null>(null);

export function UploadQueueProvider({
  children,
  queue: suppliedQueue,
}: {
  children: ReactNode;
  queue?: UploadQueue;
}): React.ReactElement {
  const [queue] = useState(() => suppliedQueue ?? createBrowserUploadQueue());

  useEffect(() => {
    const syncVisibility = (): void => queue.setPollingPaused(document.visibilityState === "hidden");
    syncVisibility();
    queue.start();
    document.addEventListener("visibilitychange", syncVisibility);
    return () => {
      document.removeEventListener("visibilitychange", syncVisibility);
      queue.stop();
    };
  }, [queue]);

  return <UploadQueueContext value={queue}>{children}</UploadQueueContext>;
}

export interface UploadQueueContextValue {
  items: readonly UploadQueueItem[];
  summary: ReturnType<typeof queueSummary>;
  enqueue: (files: Iterable<File>, intent: ExplicitIntakeIntent) => string[];
  cancel: (clientId: string) => boolean;
  retryUpload: (clientId: string) => boolean;
  retryIntake: (clientId: string) => Promise<void>;
  reprocessIntake: (clientId: string) => Promise<void>;
}

export function useUploadQueue(): UploadQueueContextValue {
  const queue = useContext(UploadQueueContext);
  if (!queue) throw new Error("useUploadQueue must be used inside UploadQueueProvider.");
  const items = useSyncExternalStore(queue.subscribe, queue.getSnapshot, queue.getSnapshot);
  return useMemo(() => ({
    items,
    summary: queueSummary(items),
    enqueue: (files: Iterable<File>, intent: ExplicitIntakeIntent) => queue.enqueue(files, intent),
    cancel: (clientId: string) => queue.cancel(clientId),
    retryUpload: (clientId: string) => queue.retryUpload(clientId),
    retryIntake: (clientId: string) => queue.retryIntake(clientId),
    reprocessIntake: (clientId: string) => queue.reprocessIntake(clientId),
  }), [items, queue]);
}
