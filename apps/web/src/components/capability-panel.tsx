"use client";

import { useEffect, useState } from "react";
import {
  fetchMcpServers,
  fetchSkills,
  type McpView,
  type SkillLibrary,
} from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";


const ROLE_LABELS: Record<string, string> = { lead: "대장", worker: "하위" };

/** One row per server: which roles actually have it, and — only when no role
 *  has it — why not.
 *
 *  This panel used to ask for the worker's servers alone, so `ask` (the lead's
 *  own server, the one it reaches the user with) was listed as inactive with
 *  "lead 역할 범위 밖" — indistinguishable from github sitting there disabled for
 *  want of a token. A server that is doing its job should not look broken. */
function mergeRoles(views: McpView[]): {
  name: string;
  detail: string;
  roles: string[];
  reason: string;
}[] {
  const rows = new Map<string, { name: string; detail: string; roles: string[]; reason: string }>();
  for (const view of views) {
    for (const server of view.active) {
      const row = rows.get(server.name) ?? {
        name: server.name,
        detail: server.description || server.command,
        roles: [],
        reason: "",
      };
      row.detail = server.description || server.command || row.detail;
      row.roles = [...row.roles, view.role];
      rows.set(server.name, row);
    }
    for (const server of view.inactive) {
      const row = rows.get(server.name) ?? {
        name: server.name,
        detail: "",
        roles: [],
        reason: server.reason,
      };
      // Being out of scope for one role is not a reason to show: another role
      // may have it. Anything else (a missing secret, a missing binary) is.
      if (!/역할 범위 밖/.test(server.reason)) row.reason = server.reason;
      rows.set(server.name, row);
    }
  }
  return [...rows.values()];
}


/** What the agents can reach: the MCP servers a sub-agent gets and the skill
 *  library it can follow. Read-only on purpose — both are files
 *  (`config/mcp.yaml`, `packages/skills/library/`) that are re-read when they
 *  change, and an editor here would be a second source of truth. */
export function CapabilityPanel() {
  const [mcp, setMcp] = useState<McpView[] | null>(null);
  const [skills, setSkills] = useState<SkillLibrary | null>(null);

  useEffect(() => {
    void Promise.all(
      ["lead", "worker"].map((role) => fetchMcpServers(role).catch(() => null))
    )
      .then((views) => setMcp(views.filter((view): view is McpView => view !== null)))
      .catch(() => setMcp(null));
    void fetchSkills().then(setSkills, () => setSkills(null));
  }, []);

  return (
    <FieldGroup>
      <Field>
        <FieldLabel>MCP 서버</FieldLabel>
        <FieldDescription>
          <code>config/mcp.yaml</code>에서 정의합니다. 도구는 하위 에이전트 몫이고, 대장이
          받는 서버는 사용자에게 질문하는 <code>ask</code> 하나뿐입니다. 범위 밖 서버 호출은
          게이트가 거부합니다.
        </FieldDescription>
        {mcp === null ? (
          <FieldDescription>불러오지 못했습니다.</FieldDescription>
        ) : (
          <div className="flex flex-col gap-1.5 text-xs">
            {mergeRoles(mcp).map((server) => (
              <div
                key={server.name}
                className={`flex flex-wrap items-center gap-2 ${
                  server.roles.length === 0 ? "opacity-60" : ""
                }`}
              >
                <Badge variant={server.roles.length ? "secondary" : "outline"}>
                  {server.name}
                </Badge>
                {server.roles.map((role) => (
                  <Badge key={role} variant="outline">
                    {ROLE_LABELS[role] ?? role}
                  </Badge>
                ))}
                <span className="text-muted-foreground">
                  {server.roles.length ? server.detail : server.reason || "사용할 수 없음"}
                </span>
              </div>
            ))}
            {mergeRoles(mcp).length === 0 && (
              <span className="text-muted-foreground">등록된 서버가 없습니다.</span>
            )}
          </div>
        )}
      </Field>

      <Field>
        <FieldLabel>스킬 라이브러리</FieldLabel>
        <FieldDescription>
          <code>packages/skills/library/</code> 의 <code>*.yaml</code>·<code>*.md</code>
          (SKILL.md 형식 그대로) 및 상태 디렉토리의 사용자 스킬. 필요한 MCP 서버가 실제로
          붙어 있는 스킬만 에이전트 목록에 올라갑니다.
        </FieldDescription>
        {skills === null ? (
          <FieldDescription>불러오지 못했습니다.</FieldDescription>
        ) : (
          <div className="flex flex-col gap-1.5 text-xs">
            {skills.skills.map((skill) => (
              <div key={skill.name} className="flex flex-wrap items-center gap-2">
                <Badge variant="secondary">{skill.name}</Badge>
                <span className="text-muted-foreground">{skill.description}</span>
                {skill.mcp.map((server) => (
                  <Badge key={server} variant="outline">
                    MCP: {server}
                  </Badge>
                ))}
              </div>
            ))}
            {skills.skills.length === 0 && (
              <span className="text-muted-foreground">스킬이 없습니다.</span>
            )}
            {/* A skill file that fails validation is invisible to the agent;
                this is the only place anyone would find out. */}
            {skills.errors.map((error) => (
              <div key={error.path} className="text-destructive">
                {error.path}: {error.message}
              </div>
            ))}
          </div>
        )}
      </Field>
    </FieldGroup>
  );
}
