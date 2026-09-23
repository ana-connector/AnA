"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ArrowLeftIcon, TrashIcon } from "lucide-react";
import { apiBase } from "@/lib/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Item, ItemActions, ItemContent, ItemTitle } from "@/components/ui/item";
import { Skeleton } from "@/components/ui/skeleton";
import { PageShell } from "@/components/page-shell";

type SecretRow = { scope: string; name: string; updated_at: string };

export default function SecretsPage() {
  const [secrets, setSecrets] = useState<SecretRow[] | null>(null);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [scope, setScope] = useState("global");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${apiBase()}/secrets`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setSecrets(((await response.json()) as { secrets: SecretRow[] }).secrets);
      setError(null);
    } catch (e) {
      setSecrets([]);
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim() || !value) return;
    const response = await fetch(`${apiBase()}/secrets`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: name.trim(), value, scope: scope.trim() || "global" }),
    });
    if (!response.ok) {
      setError(
        response.status === 422
          ? "이름은 환경변수 형식이어야 함 (영문자/숫자/밑줄, 숫자로 시작 불가)"
          : `저장 실패: HTTP ${response.status}`
      );
      return;
    }
    // Cleared immediately: the value is never echoed back by the server, and
    // leaving it in the field is one more place it can be seen.
    setName("");
    setValue("");
    setError(null);
    void refresh();
  };

  const remove = async (row: SecretRow) => {
    await fetch(
      `${apiBase()}/secrets/${encodeURIComponent(row.scope)}/${encodeURIComponent(row.name)}`,
      { method: "DELETE" }
    );
    void refresh();
  };

  const invalidName = name.length > 0 && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name);

  return (
    <PageShell><div className="mx-auto flex w-full max-w-2xl flex-col gap-4 p-6">
      <header className="flex items-center gap-2">
        <Button size="xs" variant="ghost" render={<a href="/" />}>
          <ArrowLeftIcon data-icon="inline-start" />
          채팅
        </Button>
        <span className="font-heading text-sm font-semibold">Secrets</span>
      </header>

      <Alert>
        <AlertTitle>값은 저장 후 다시 볼 수 없음</AlertTitle>
        <AlertDescription>
          등록한 값은 두 CLI의 환경변수로 주입됨. 이 값이 그대로 외부로 나가는 명령은 게이트가
          차단하고, 감사 로그에는 마스킹해 기록함.
        </AlertDescription>
      </Alert>

      <form onSubmit={submit}>
        <FieldGroup>
          <Field data-invalid={invalidName || undefined}>
            <FieldLabel htmlFor="secret-name">이름</FieldLabel>
            <Input
              id="secret-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="GITHUB_TOKEN"
              aria-invalid={invalidName || undefined}
            />
            <FieldDescription>에이전트에 주입될 환경변수 이름</FieldDescription>
          </Field>

          <Field>
            <FieldLabel htmlFor="secret-scope">스코프</FieldLabel>
            <Input
              id="secret-scope"
              value={scope}
              onChange={(e) => setScope(e.target.value)}
              placeholder="global"
            />
            <FieldDescription>global = 모든 대화, 그 외 = 해당 프로젝트</FieldDescription>
          </Field>

          <Field>
            <FieldLabel htmlFor="secret-value">값</FieldLabel>
            <Input
              id="secret-value"
              type="password"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="저장 후 표시되지 않음"
            />
          </Field>

          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <Field orientation="horizontal">
            <Button type="submit" disabled={invalidName}>
              저장
            </Button>
          </Field>
        </FieldGroup>
      </form>

      <div className="flex flex-col gap-2">
        {secrets === null ? (
          <>
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
          </>
        ) : secrets.length === 0 ? (
          <Empty>
            <EmptyHeader>
              <EmptyTitle>등록된 시크릿 없음</EmptyTitle>
              <EmptyDescription>위에서 추가하면 목록에 나타남</EmptyDescription>
            </EmptyHeader>
          </Empty>
        ) : (
          secrets.map((row) => (
            <Item key={`${row.scope}/${row.name}`} variant="outline">
              <ItemContent>
                <ItemTitle className="font-mono">{row.name}</ItemTitle>
                <span className="text-xs text-muted-foreground">
                  {new Date(row.updated_at).toLocaleString()}
                </span>
              </ItemContent>
              <ItemActions>
                <Badge variant="secondary">{row.scope}</Badge>
                <Button size="icon-sm" variant="destructive" onClick={() => remove(row)}>
                  <TrashIcon />
                </Button>
              </ItemActions>
            </Item>
          ))
        )}
      </div>
    </div></PageShell>
  );
}
