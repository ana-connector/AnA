"use client";

import { useEffect, useState, type FormEvent } from "react";
import { BoxIcon } from "lucide-react";
import {
  fetchWorkspaceCapabilities,
  updateWorkspace,
  type PortMapping,
  type WorkspaceSummary,
} from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";

function portsToText(ports: PortMapping[]): string {
  return ports.map((p) => (p.host === p.container ? `${p.host}` : `${p.host}:${p.container}`)).join(", ");
}

function textToPorts(text: string): PortMapping[] {
  return text
    .split(/[,\s]+/)
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => {
      const [host, container] = entry.split(":");
      return { host: Number(host), container: Number(container ?? host) };
    })
    .filter((p) => Number.isFinite(p.host) && Number.isFinite(p.container));
}

/** Container settings for one workspace: its own image and published ports, so
 *  a dev server the agent starts inside it is reachable from the browser.
 *
 *  Nothing starts here — the container comes up the next time the workspace is
 *  used, which keeps a half-started container from outliving a settings edit. */
export function WorkspaceSettings({
  workspace,
  onSaved,
  onForget,
}: {
  workspace: WorkspaceSummary;
  onSaved: () => void;
  /** Unregisters the workspace. Lives here rather than as a second row action:
   *  one icon per item stays legible, and removal is not a one-click thing. */
  onForget: () => void;
}) {
  const [enabled, setEnabled] = useState(workspace.container_enabled);
  const [image, setImage] = useState(workspace.container_image ?? "");
  const [ports, setPorts] = useState(portsToText(workspace.container_ports ?? []));
  const [available, setAvailable] = useState<boolean | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    void fetchWorkspaceCapabilities().then((caps) => setAvailable(caps.containers));
  }, []);

  useEffect(() => {
    setEnabled(workspace.container_enabled);
    setImage(workspace.container_image ?? "");
    setPorts(portsToText(workspace.container_ports ?? []));
  }, [workspace]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    await updateWorkspace(workspace.id, {
      container_enabled: enabled,
      container_image: image.trim() || null,
      container_ports: textToPorts(ports),
    });
    setSaved(true);
    onSaved();
  };

  return (
    <form onSubmit={submit} className="px-2 pb-2">
      <FieldGroup>
        <Field orientation="horizontal">
          <FieldLabel htmlFor="ws-container" className="flex items-center gap-1.5">
            <BoxIcon className="size-3.5" />
            전용 컨테이너
          </FieldLabel>
          <Switch
            id="ws-container"
            checked={enabled}
            onCheckedChange={setEnabled}
            disabled={available === false}
          />
        </Field>

        {available === false && (
          <FieldDescription>
            서버에 Docker 소켓이 없어 사용할 수 없음. compose에서
            <code className="mx-1 font-mono">/var/run/docker.sock</code>를 마운트하고
            <code className="mx-1 font-mono">ANA_WORKSPACE_CONTAINERS=on</code>을 설정해야 함.
          </FieldDescription>
        )}

        <Field>
          <FieldLabel htmlFor="ws-image">이미지</FieldLabel>
          <Input
            id="ws-image"
            value={image}
            onChange={(e) => setImage(e.target.value)}
            placeholder="ana-server:latest"
            disabled={!enabled}
          />
          <FieldDescription>비우면 두 CLI가 들어 있는 ANA 서버 이미지를 씀</FieldDescription>
        </Field>

        <Field>
          <FieldLabel htmlFor="ws-ports">포트 포워딩</FieldLabel>
          <Input
            id="ws-ports"
            value={ports}
            onChange={(e) => setPorts(e.target.value)}
            placeholder="5173, 8080:80"
            disabled={!enabled}
          />
          <FieldDescription>
            호스트:컨테이너. 에이전트가 안에서 띄운 서버를 브라우저로 열 수 있음.
          </FieldDescription>
        </Field>

        <Field orientation="horizontal">
          <Button type="submit" size="sm" disabled={available === false}>
            저장
          </Button>
          {saved && <Badge variant="secondary">다음 턴부터 적용</Badge>}
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="ml-auto text-destructive"
            title="목록에서 제거 (폴더는 그대로)"
            onClick={onForget}
          >
            제거
          </Button>
        </Field>
      </FieldGroup>
    </form>
  );
}
