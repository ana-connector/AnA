"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchSettings, saveSettings } from "@/lib/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  ClaudeIcon,
  CursorIcon,
  KimiIcon,
  OpenAIIcon,
  OpenCodeIcon,
  TapieIcon,
} from "@/components/agent-icons";
import { AGENT_LABELS } from "@/components/ui/agent-toggle";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { notify } from "@/lib/notify";

const AGENT_ICON: Record<string, (props: { className?: string }) => React.ReactElement> = {
  claude: ClaudeIcon,
  codex: OpenAIIcon,
  kimi: KimiIcon,
  tapie: TapieIcon,
  cursor: CursorIcon,
  opencode: OpenCodeIcon,
};

/** What each CLI needs before it can answer anything, so "off" can be a
 *  decision rather than a workaround for a CLI that was never set up. */
const AGENT_NOTE: Record<string, string> = {
  claude: "Claude Pro 구독 로그인. 기본 에이전트라 끌 수 없습니다.",
  codex: "~/.codex 로그인",
  kimi: "~/.kimi-code 로그인",
  tapie: "TAPIE_API_KEY",
  cursor: "~/.config/cursor 로그인",
  opencode: "opencode providers 로그인 · 한도 API 없음",
};

/** Which CLIs ANA may use at all.
 *
 *  The ladder answers "which CLI next"; this answers "which CLI ever". The two
 *  are separate because the reasons differ: a rung is a budget decision, while
 *  switching a CLI off is usually about an account that is not set up, a CLI
 *  that is not installed, or a provider the user does not want this machine
 *  talking to. Off means off everywhere — the router skips it, failover skips
 *  it, the composer stops offering it, no prepared home or MCP config is
 *  written for it, and it leaves the status bar.
 *
 *  Its own save, like the governance switch: a flip here changes where the
 *  next turn goes, and that should not be waiting behind an unrelated edit to
 *  the system prompt. */
export function AgentSwitches() {
  const [agents, setAgents] = useState<string[] | null>(null);
  const [disabled, setDisabled] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const loadState = useRef({ mounted: false, request: 0 });

  const load = useCallback(() => {
    const request = ++loadState.current.request;
    return fetchSettings().then(
      (settings) => {
        if (!loadState.current.mounted || loadState.current.request !== request) return;
        setAgents(settings.agents ?? []);
        setDisabled(settings.disabled_agents ?? []);
      },
      (e) => {
        if (!loadState.current.mounted || loadState.current.request !== request) return;
        setAgents([]);
        setError(e instanceof Error ? e.message : String(e));
      },
    );
  }, []);

  useEffect(() => {
    const state = loadState.current;
    state.mounted = true;
    void load();
    return () => {
      state.mounted = false;
      state.request++;
    };
  }, [load]);

  const toggle = async (agent: string, on: boolean) => {
    const next = on
      ? disabled.filter((name) => name !== agent)
      : [...disabled.filter((name) => name !== agent), agent];
    setSaving(agent);
    try {
      const stored = await saveSettings({ disabled_agents: next });
      setDisabled(stored.disabled_agents ?? next);
      setError(null);
      notify.success(
        `${AGENT_LABELS[agent as keyof typeof AGENT_LABELS] ?? agent} ${on ? "켜짐" : "꺼짐"}`,
        on
          ? "다음 턴부터 라우팅 대상에 포함됩니다."
          : "다음 턴부터 라우팅·폴백·수동 선택에서 제외됩니다.",
      );
    } catch (e) {
      // The server rejects a list that would leave nothing on, so the switch
      // goes back to what is actually stored rather than lying.
      setError(e instanceof Error ? e.message : String(e));
      await load();
    } finally {
      setSaving(null);
    }
  };

  if (agents === null) {
    return (
      <div className="flex flex-col gap-3">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  const off = new Set(disabled);

  return (
    <FieldGroup>
      {error ? (
        <Alert variant="destructive">
          <AlertTitle>저장하지 못했습니다</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      {agents.map((agent) => {
        const Icon = AGENT_ICON[agent];
        const on = !off.has(agent);
        // claude is the default agent — where an unknown agent name and an
        // all-disabled ladder both land — so the server refuses to store it
        // as off and the switch says so rather than failing the save.
        const locked = agent === "claude";
        return (
          <Field key={agent} orientation="horizontal">
            <FieldLabel htmlFor={`agent-${agent}`} className="flex items-center gap-1.5">
              {Icon ? <Icon className="size-3.5" /> : null}
              {AGENT_LABELS[agent as keyof typeof AGENT_LABELS] ?? agent}
              <span className="text-xs font-normal text-muted-foreground">
                {AGENT_NOTE[agent] ?? ""}
              </span>
            </FieldLabel>
            <Switch
              id={`agent-${agent}`}
              checked={on}
              disabled={locked || saving === agent}
              onCheckedChange={(next) => void toggle(agent, next)}
            />
          </Field>
        );
      })}

      <FieldDescription>
        끈 CLI는 라우팅 래더에서 건너뛰고, 한도에 걸렸을 때의 폴백 대상에서도 빠지고, 입력창의
        수동 선택 목록에도 나오지 않습니다. 래더의 규칙은 지워지지 않으니 다시 켜면 그대로
        돌아옵니다. 기본 에이전트인 Claude Code는 끌 수 없습니다 — 알 수 없는 에이전트
        이름과 래더가 전부 꺼진 경우가 모두 여기로 떨어집니다.
      </FieldDescription>
    </FieldGroup>
  );
}
