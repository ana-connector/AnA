"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { PlusIcon, TrashIcon } from "lucide-react";
import { apiBase } from "@/lib/api";
import { timeAgo, timeExact } from "@/lib/format";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { notify } from "@/lib/notify";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { PageShell } from "@/components/page-shell";

type SecretRow = { scope: string; name: string; updated_at: string };

export default function SecretsPage() {
  const [secrets, setSecrets] = useState<SecretRow[] | null>(null);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [scope, setScope] = useState("global");
  const [error, setError] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<SecretRow | null>(null);
  // Whether what is stored here survives a restart. It does not when
  // ANA_SECRET_KEY is unset: the cipher takes a random key per process.
  const [keyPersistent, setKeyPersistent] = useState(true);
  const loadState = useRef({ mounted: false, request: 0 });

  const refresh = useCallback(() => {
    const request = ++loadState.current.request;
    return fetch(`${apiBase()}/secrets`).then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json() as Promise<{
        secrets: SecretRow[];
        key_persistent?: boolean;
      }>;
    }).then(
      (body) => {
        if (!loadState.current.mounted || loadState.current.request !== request) return;
        setSecrets(body.secrets);
        setKeyPersistent(body.key_persistent !== false);
        setError(null);
      },
      (e) => {
        if (!loadState.current.mounted || loadState.current.request !== request) return;
        setSecrets([]);
        setError(String(e));
      },
    );
  }, []);

  useEffect(() => {
    const state = loadState.current;
    state.mounted = true;
    void refresh();
    return () => {
      state.mounted = false;
      state.request++;
    };
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
          ? "이름은 환경변수 형식이어야 합니다 (영문자/숫자/밑줄, 숫자로 시작 불가)"
          : `저장하지 못했습니다: HTTP ${response.status}`
      );
      return;
    }
    notify.success(`${name.trim()} 저장됨`, "다음 턴부터 두 CLI의 환경변수로 주입됩니다.");
    // Cleared immediately: the value is never echoed back by the server, and
    // leaving it in the field is one more place it can be seen.
    setName("");
    setValue("");
    setError(null);
    void refresh();
  };

  const remove = async (row: SecretRow) => {
    setPendingDelete(null);
    const response = await fetch(
      `${apiBase()}/secrets/${encodeURIComponent(row.scope)}/${encodeURIComponent(row.name)}`,
      { method: "DELETE" }
    );
    if (response.ok) notify.success(`${row.name} 삭제됨`);
    else notify.error("삭제하지 못했습니다", `HTTP ${response.status}`);
    void refresh();
  };

  const invalidName = name.length > 0 && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name);

  return (
    <PageShell
      title="시크릿"
      description="등록한 값은 두 CLI의 환경변수로 주입됩니다. 그 값을 그대로 밖으로 내보내는 명령은 게이트가 차단하고, 감사 로그에는 마스킹해 기록합니다."
    >
      <Alert>
        <AlertTitle>값은 저장한 뒤 다시 볼 수 없습니다</AlertTitle>
        <AlertDescription>
          서버는 값을 암호화해 저장하고 되돌려주지 않습니다. 바꾸려면 같은 이름으로 다시
          저장하세요.
        </AlertDescription>
      </Alert>

      <Card>
        <CardHeader>
          <CardTitle>시크릿 추가</CardTitle>
          <CardDescription>에이전트에게는 이름만 알려주면 됩니다.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit}>
            <FieldGroup>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field data-invalid={invalidName || undefined}>
                  <FieldLabel htmlFor="secret-name">이름</FieldLabel>
                  <Input
                    id="secret-name"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="GITHUB_TOKEN"
                    aria-invalid={invalidName || undefined}
                    className="font-mono"
                  />
                  <FieldDescription>
                    {invalidName
                      ? "영문자/숫자/밑줄만, 숫자로 시작할 수 없습니다."
                      : "주입될 환경변수 이름"}
                  </FieldDescription>
                </Field>

                <Field>
                  <FieldLabel htmlFor="secret-scope">스코프</FieldLabel>
                  <Input
                    id="secret-scope"
                    value={scope}
                    onChange={(e) => setScope(e.target.value)}
                    placeholder="global"
                    className="font-mono"
                  />
                  <FieldDescription>global = 모든 대화, 그 외 = 해당 프로젝트</FieldDescription>
                </Field>
              </div>

              <Field>
                <FieldLabel htmlFor="secret-value">값</FieldLabel>
                <Input
                  id="secret-value"
                  type="password"
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  placeholder="저장 후에는 표시되지 않습니다"
                />
              </Field>

              {error ? (
                <Alert variant="destructive">
                  <AlertDescription>{error}</AlertDescription>
                </Alert>
              ) : null}

              <Field orientation="horizontal">
                <Button type="submit" disabled={invalidName || !name.trim() || !value}>
                  <PlusIcon data-icon="inline-start" />
                  저장
                </Button>
              </Field>
            </FieldGroup>
          </form>
        </CardContent>
      </Card>

      {!keyPersistent ? (
        <Alert variant="destructive">
          <AlertTitle>저장한 값이 재시작 후 사라집니다</AlertTitle>
          <AlertDescription>
            <code>ANA_SECRET_KEY</code>가 설정돼 있지 않아 값이 프로세스마다 새로 만들어지는
            임시 키로 암호화됩니다. 서버가 재시작되면 여기 적힌 이름은 남고 값만 읽을 수 없게
            되며, 에이전트는 키가 없다고 말합니다. <code>.env</code>에 고정 키를 넣고 다시
            시작한 뒤 값을 다시 입력하세요.
          </AlertDescription>
        </Alert>
      ) : null}

      {secrets === null ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : secrets.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>등록된 시크릿 없음</EmptyTitle>
            <EmptyDescription>위에서 추가하면 목록에 나타납니다.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>이름</TableHead>
              <TableHead className="w-32">스코프</TableHead>
              <TableHead className="w-28">수정</TableHead>
              <TableHead className="w-12" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {secrets.map((row) => (
              <TableRow key={`${row.scope}/${row.name}`}>
                <TableCell className="font-mono">{row.name}</TableCell>
                <TableCell>
                  <Badge variant="secondary">{row.scope}</Badge>
                </TableCell>
                <TableCell className="text-muted-foreground">
                  <Tooltip>
                    <TooltipTrigger render={<span className="whitespace-nowrap" />}>
                      {timeAgo(row.updated_at)}
                    </TooltipTrigger>
                    <TooltipContent>{timeExact(row.updated_at)}</TooltipContent>
                  </Tooltip>
                </TableCell>
                <TableCell>
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    onClick={() => setPendingDelete(row)}
                    aria-label={`${row.name} 삭제`}
                  >
                    <TrashIcon />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {/* Deleting a credential breaks whatever was using it, and the value
          cannot be recovered from here — worth one confirmation. */}
      <AlertDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => !open && setPendingDelete(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{pendingDelete?.name} 을(를) 삭제할까요?</AlertDialogTitle>
            <AlertDialogDescription>
              이 값을 쓰던 에이전트 작업은 다음 턴부터 실패합니다. 값은 복구할 수 없고, 다시
              등록해야 합니다.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>취소</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => pendingDelete && void remove(pendingDelete)}
            >
              삭제
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PageShell>
  );
}
