"use client";

import { useEffect, useState } from "react";
import { fetchModels, type LadderModel } from "@/lib/api";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export const AGENTS = ["claude", "codex"] as const;
export type Agent = (typeof AGENTS)[number];
/** "auto" is not an agent: it hands the choice to the ladder. */
export type AgentChoice = Agent | "auto";
export const AGENT_CHOICES = ["auto", ...AGENTS] as const;

export const AGENT_LABELS: Record<Agent, string> = {
  claude: "Claude Code",
  codex: "Codex",
};

/** Which model answers the next turn.
 *
 *  Auto means the ladder decides by how much of each window is spent — which
 *  is the normal case. The manual options are the ladder's own rungs rather
 *  than CLI names: "claude-opus-5" is what a person thinks in, and offering
 *  something the router could not have chosen would be a different feature. */
export function AgentToggle({
  value,
  onChange,
  disabled,
}: {
  value: AgentChoice;
  onChange: (agent: AgentChoice) => void;
  disabled?: boolean;
}) {
  const [models, setModels] = useState<LadderModel[]>([]);

  useEffect(() => {
    void fetchModels().then(setModels);
  }, []);

  // One entry per agent: picking a model pins the CLI, and the ladder supplies
  // that CLI's model on the server side.
  const byAgent = new Map<string, LadderModel>();
  for (const entry of models) {
    if (!byAgent.has(entry.agent)) byAgent.set(entry.agent, entry);
  }

  return (
    <Select
      value={value}
      onValueChange={(next) => {
        if (next === "auto" || next === "claude" || next === "codex") onChange(next);
      }}
      disabled={disabled}
    >
      <SelectTrigger size="sm" className="w-44" aria-label="모델">
        <SelectValue placeholder="Auto" />
      </SelectTrigger>
      <SelectContent>
        <SelectGroup>
          <SelectItem value="auto">Auto (사용량 기준)</SelectItem>
          {AGENTS.map((agent) => {
            const entry = byAgent.get(agent);
            return (
              <SelectItem key={agent} value={agent}>
                {entry?.model ?? AGENT_LABELS[agent]}
              </SelectItem>
            );
          })}
        </SelectGroup>
      </SelectContent>
    </Select>
  );
}
