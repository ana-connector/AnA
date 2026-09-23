"use client";

import { useEffect, useState } from "react";
import { apiBase } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";

type Server = { name: string; description: string; command: string };
type Inactive = { name: string; reason: string };
type McpView = { role: string; enabled: boolean; active: Server[]; inactive: Inactive[] };

type Skill = {
  name: string;
  title: string;
  description: string;
  roles: string[];
  mcp: string[];
};
type SkillView = { skills: Skill[]; errors: { path: string; message: string }[] };

/** What the agents can reach: the MCP servers a sub-agent gets and the skill
 *  library it can follow. Read-only on purpose — both are files
 *  (`config/mcp.yaml`, `packages/skills/library/`) that are re-read when they
 *  change, and an editor here would be a second source of truth. */
export function CapabilityPanel() {
  const [mcp, setMcp] = useState<McpView | null>(null);
  const [skills, setSkills] = useState<SkillView | null>(null);

  useEffect(() => {
    void fetch(`${apiBase()}/mcp/servers?role=worker`)
      .then((response) => (response.ok ? response.json() : null))
      .then(setMcp)
      .catch(() => setMcp(null));
    void fetch(`${apiBase()}/skills`)
      .then((response) => (response.ok ? response.json() : null))
      .then(setSkills)
      .catch(() => setSkills(null));
  }, []);

  return (
    <FieldGroup>
      <Field>
        <FieldLabel>하위 에이전트가 쓰는 MCP 서버</FieldLabel>
        <FieldDescription>
          <code>config/mcp.yaml</code>에서 정의합니다. 대장은 도구를 쓰지 않으므로 서버는
          하위 에이전트 몫이고, 범위 밖 서버 호출은 게이트가 거부합니다.
        </FieldDescription>
        {mcp === null ? (
          <FieldDescription>불러오지 못했습니다.</FieldDescription>
        ) : (
          <div className="flex flex-col gap-1.5 text-xs">
            {mcp.active.map((server) => (
              <div key={server.name} className="flex items-center gap-2">
                <Badge variant="secondary">{server.name}</Badge>
                <span className="text-muted-foreground">{server.description || server.command}</span>
              </div>
            ))}
            {mcp.inactive.map((server) => (
              <div key={server.name} className="flex items-center gap-2 opacity-60">
                <Badge variant="outline">{server.name}</Badge>
                <span className="text-muted-foreground">{server.reason}</span>
              </div>
            ))}
            {mcp.active.length === 0 && mcp.inactive.length === 0 && (
              <span className="text-muted-foreground">등록된 서버가 없습니다.</span>
            )}
          </div>
        )}
      </Field>

      <Field>
        <FieldLabel>스킬 라이브러리</FieldLabel>
        <FieldDescription>
          <code>packages/skills/library/*.yaml</code> 및 상태 디렉토리의 사용자 스킬.
          필요한 MCP 서버가 실제로 붙어 있는 스킬만 에이전트 목록에 올라갑니다.
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
