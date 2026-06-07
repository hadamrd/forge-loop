// DrawerHost — renders the active right-side detail drawer body inside the Drawer primitive.
import { Drawer } from "@/components/primitives";
import { useDrawerState } from "@/lib/drawer";
import { EventDrawerBody, WorkerDrawerBody, PrDrawerBody, SagaDrawerBody } from "./index";
import type { EventEnvelope } from "@/domain/events";
import type { Worker, PullRequest, Saga } from "@/domain/models";

export function DrawerHost() {
  const { current, close } = useDrawerState();
  const width = current?.kind === "pr" ? 560 : 520;
  return (
    <Drawer open={!!current} onClose={close} width={width} label="detail">
      {current?.kind === "event" && <EventDrawerBody event={current.data as EventEnvelope} />}
      {current?.kind === "worker" && <WorkerDrawerBody worker={current.data as Worker} />}
      {current?.kind === "pr" && <PrDrawerBody pr={current.data as PullRequest} />}
      {current?.kind === "saga" && <SagaDrawerBody saga={current.data as Saga} />}
    </Drawer>
  );
}
