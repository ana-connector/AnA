"use client";

import { useMemo, useState } from "react";
import { ChevronsUpDownIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { groupByProvider, modelLabel } from "@/lib/models";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";

/** A model picker for a list too long to scroll.
 *
 *  A dropdown is the right shape for the four models a Claude rung can take.
 *  It is the wrong shape for OpenCode, which is one CLI in front of several
 *  providers and offers dozens of `provider/model` names at once: finding
 *  `opencode-go/kimi-k3` in a flat list of seventy means knowing where it
 *  sits. So the long case gets a search box, and the names stay grouped by
 *  provider — the grouping the CLI itself prints them in, and the thing a
 *  person actually chooses first ("the free one", "the one on my own
 *  gateway").
 *
 *  The grouping rule itself is in `@/lib/models`, where it can be checked
 *  without a browser. */
export function ModelPicker({
  value,
  models,
  onChange,
  onCustom,
  label,
  defaultLabel = "CLI 기본값",
  customLabel = "직접 입력…",
}: {
  value: string;
  models: string[];
  onChange: (model: string) => void;
  onCustom: () => void;
  /** Spoken name of the field, for the trigger's accessible label. */
  label: string;
  defaultLabel?: string;
  customLabel?: string;
}) {
  const [open, setOpen] = useState(false);

  // Insertion order, not alphabetical: the server hands the names over in the
  // order the CLI listed them, which already groups them by provider.
  const groups = useMemo(() => groupByProvider(models), [models]);

  const choose = (model: string) => {
    setOpen(false);
    onChange(model);
  };

  return (
    <>
      <Button
        type="button"
        variant="outline"
        aria-label={label}
        className="justify-between font-mono font-normal"
        onClick={() => setOpen(true)}
      >
        <span className="truncate">{value || defaultLabel}</span>
        <ChevronsUpDownIcon className="size-4 shrink-0 opacity-50" />
      </Button>
      <CommandDialog
        open={open}
        onOpenChange={setOpen}
        title={label}
        description="모델 이름으로 검색합니다"
      >
        <Command>
          <CommandInput placeholder="모델 검색 (프로바이더 이름도 찾습니다)" autoFocus />
          <CommandList>
            <CommandEmpty>그런 이름의 모델이 없습니다.</CommandEmpty>
            <CommandGroup>
              <CommandItem value={defaultLabel} onSelect={() => choose("")}>
                {defaultLabel}
              </CommandItem>
              <CommandItem
                value={customLabel}
                onSelect={() => {
                  setOpen(false);
                  onCustom();
                }}
              >
                {customLabel}
              </CommandItem>
            </CommandGroup>
            {groups.map(({ provider, models: names }) => (
              <CommandGroup key={provider || "_"} heading={provider || "모델"}>
                {names.map((name) => (
                  <CommandItem
                    key={name}
                    // The whole name is the search text, so typing a provider
                    // narrows to its models and typing a model finds it
                    // whichever provider carries it.
                    value={name}
                    data-checked={name === value}
                    className="font-mono"
                    onSelect={() => choose(name)}
                  >
                    {modelLabel(name, provider)}
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </CommandDialog>
    </>
  );
}
